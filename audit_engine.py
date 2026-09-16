import io
import json
import logging
import time
from datetime import datetime
from urllib.parse import urlparse, urljoin, urldefrag

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib import colors

logger = logging.getLogger(__name__)

# Wie viele Seiten je Paket tatsaechlich gecrawlt werden.
# Diese Zahlen muessen mit dem uebereinstimmen, was auf der Verkaufsseite steht.
PAGE_LIMITS = {'quick': 10, 'standard': 20, 'pro': 60}

SECURITY_HEADERS = [
    'Content-Security-Policy',
    'Strict-Transport-Security',
    'X-Content-Type-Options',
    'X-Frame-Options',
    'Referrer-Policy',
    'Permissions-Policy',
]

# Oeffentlich abrufbare Pfade, die nicht oeffentlich sein sollten.
# Reines GET auf oeffentliche URLs, kein Eindringen.
# Jeder Pfad braucht einen Inhaltsnachweis. Ein Status 200 allein genuegt nicht:
# viele Seiten liefern fuer unbekannte Pfade ihre normale HTML-Seite mit Status 200.
def _ist_env(b):        return b'=' in b[:400] and b'<html' not in b[:400].lower()
def _ist_gitconfig(b):  return b'[core]' in b[:400]
def _ist_zip(b):        return b[:2] == b'PK'
def _ist_dsstore(b):    return b[:8] == b'\x00\x00\x00\x01Bud1'
def _ist_wpconfig(b):   return b'DB_PASSWORD' in b[:2000] or b'<?php' in b[:200]
def _ist_serverstatus(b): return b'Apache Server Status' in b[:2000]
def _ist_phpinfo(b):    return b'phpinfo()' in b[:4000] or b'PHP Version' in b[:4000]

SENSITIVE_PATHS = [
    ('/.env', _ist_env),
    ('/.git/config', _ist_gitconfig),
    ('/backup.zip', _ist_zip),
    ('/.DS_Store', _ist_dsstore),
    ('/wp-config.php.bak', _ist_wpconfig),
    ('/server-status', _ist_serverstatus),
    ('/phpinfo.php', _ist_phpinfo),
]

MODEL = 'claude-sonnet-5'

UA = {'User-Agent': 'HAMKODERS-Audit/1.0 (+https://hamkoders.at)'}


class WebsiteAuditBot:
    def __init__(self, anthropic_api_key, sendgrid_api_key, from_email):
        self.client = Anthropic(api_key=anthropic_api_key)
        self.anthropic_api_key = anthropic_api_key
        self.sendgrid_api_key = sendgrid_api_key
        self.from_email = from_email

    # =====================
    # ORCHESTRIERUNG
    # =====================

    def run_audit(self, website_url, customer_email, payment_id, package='standard'):
        """Vollstaendiges Audit: crawlen, analysieren, PDF, versenden."""
        try:
            if not website_url or not customer_email:
                logger.error(
                    "Audit ohne Zieldaten abgebrochen (payment %s): url=%r email=%r",
                    payment_id, website_url, customer_email
                )
                return False

            if not self._is_valid_email(customer_email):
                logger.error(f"Ungueltige E-Mail: {customer_email}")
                return False

            website_url = self._normalize_url(website_url)
            max_pages = PAGE_LIMITS.get(package, 20)
            logger.info(f"Starte {package}-Audit fuer {website_url}, bis zu {max_pages} Seiten ({payment_id})")

            data = self._collect_website_data(website_url, max_pages=max_pages)
            if not data:
                self._send_error_email(customer_email, website_url, "Website war nicht erreichbar")
                return False

            result = self._analyze_with_claude(website_url, data, package)
            if not result:
                self._send_error_email(customer_email, website_url, "Analyse fehlgeschlagen")
                return False

            pdf_bytes = self._generate_pdf_report(website_url, result)
            self._send_audit_email(customer_email, website_url, pdf_bytes)

            logger.info(f"Audit abgeschlossen: {payment_id} ({data['pages_crawled']} Seiten)")
            return True

        except Exception as e:
            logger.error(f"Audit fehlgeschlagen: {e}", exc_info=True)
            try:
                self._send_error_email(customer_email, website_url, str(e))
            except Exception:
                pass
            return False

    # =====================
    # ABRUF
    # =====================

    def _normalize_url(self, url):
        url = (url or '').strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url.rstrip('/')

    def _fetch(self, url, timeout=15):
        """Holt eine URL mit aktiver Zertifikatspruefung.

        Schlaegt die Pruefung fehl, wird das als Befund festgehalten und der
        Abruf ohne Pruefung wiederholt, damit trotzdem ein Bericht entsteht.
        """
        started = time.time()
        try:
            r = requests.get(url, headers=UA, timeout=timeout, verify=True, allow_redirects=True)
            return r, None, int((time.time() - started) * 1000)
        except requests.exceptions.SSLError as e:
            logger.warning(f"Zertifikatspruefung fehlgeschlagen fuer {url}: {e}")
            try:
                r = requests.get(url, headers=UA, timeout=timeout, verify=False, allow_redirects=True)
                return r, str(e)[:300], int((time.time() - started) * 1000)
            except requests.exceptions.RequestException as e2:
                logger.error(f"Abruf fehlgeschlagen fuer {url}: {e2}")
                return None, str(e)[:300], None
        except requests.exceptions.RequestException as e:
            logger.error(f"Abruf fehlgeschlagen fuer {url}: {e}")
            return None, None, None

    def _same_site(self, base, candidate):
        try:
            return urlparse(base).netloc.lower() == urlparse(candidate).netloc.lower()
        except Exception:
            return False

    def _collect_website_data(self, url, max_pages=20, timeout=15):
        """Crawlt die Seite bis max_pages und sammelt die Pruefdaten."""
        home, tls_error, ms = self._fetch(url, timeout)
        if home is None:
            return None

        origin = '{u.scheme}://{u.netloc}'.format(u=urlparse(home.url))
        seen = {self._canonical(home.url)}
        queue = [home.url]
        pages = []
        mixed_content = []

        while queue and len(pages) < max_pages:
            current = queue.pop(0)
            if current == home.url:
                r, err, load_ms = home, tls_error, ms
            else:
                r, err, load_ms = self._fetch(current, timeout)
            if r is None:
                continue

            html = r.text if 'text/html' in r.headers.get('Content-Type', '') else ''
            soup = BeautifulSoup(html, 'html.parser') if html else None

            title = soup.title.string.strip() if soup and soup.title and soup.title.string else ''
            desc = soup.find('meta', attrs={'name': 'description'}) if soup else None
            viewport = soup.find('meta', attrs={'name': 'viewport'}) if soup else None
            h1_count = len(soup.find_all('h1')) if soup else 0
            imgs = soup.find_all('img') if soup else []
            imgs_no_alt = sum(1 for i in imgs if not i.get('alt'))

            if current.startswith('https://') and soup:
                for tag, attr in (('script', 'src'), ('link', 'href'), ('img', 'src')):
                    for el in soup.find_all(tag):
                        src = el.get(attr) or ''
                        if src.startswith('http://'):
                            mixed_content.append(src)

            pages.append({
                'url': current,
                'status': r.status_code,
                'bytes': len(r.content),
                'load_ms': load_ms,
                'title': title,
                'title_len': len(title),
                'has_meta_description': bool(desc and desc.get('content')),
                'has_viewport': bool(viewport),
                'h1_count': h1_count,
                'images': len(imgs),
                'images_without_alt': imgs_no_alt,
            })

            if soup and len(pages) + len(queue) < max_pages * 2:
                for a in soup.find_all('a', href=True):
                    link = self._canonical(urljoin(current, a['href']))
                    if not link.startswith(('http://', 'https://')):
                        continue
                    if not self._same_site(origin, link):
                        continue
                    if any(link.lower().endswith(x) for x in ('.pdf', '.jpg', '.png', '.zip', '.svg', '.webp', '.mp4')):
                        continue
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)

        return {
            'url': home.url,
            'origin': origin,
            'status_code': home.status_code,
            'headers': dict(home.headers),
            'has_ssl': home.url.startswith('https://'),
            'tls_error': tls_error,
            'pages_crawled': len(pages),
            'pages': pages,
            'avg_load_ms': int(sum(p['load_ms'] or 0 for p in pages) / max(1, len(pages))),
            'total_bytes': sum(p['bytes'] for p in pages),
            'mixed_content': sorted(set(mixed_content))[:15],
            'robots_txt': self._check_path(origin + '/robots.txt'),
            'sitemap': self._check_path(origin + '/sitemap.xml'),
            'exposed_paths': self._check_sensitive_paths(origin),
            'security_headers': self._security_headers(home.headers),
            'html_preview': home.text[:12000],
        }

    def _canonical(self, url):
        url, _ = urldefrag(url)
        return url.rstrip('/') or url

    def _check_path(self, url, timeout=6):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, verify=True, allow_redirects=False)
            return r.status_code == 200
        except requests.exceptions.RequestException:
            return False

    def _check_sensitive_paths(self, origin, timeout=6):
        """Meldet einen Pfad nur, wenn der Inhalt ihn auch wirklich bestaetigt."""
        found = []
        for path, bestaetigt in SENSITIVE_PATHS:
            try:
                r = requests.get(origin + path, headers=UA, timeout=timeout,
                                 verify=True, allow_redirects=False)
                if r.status_code != 200 or not r.content:
                    continue
                if not bestaetigt(r.content):
                    continue
                found.append({'path': path, 'bytes': len(r.content)})
            except requests.exceptions.RequestException:
                continue
            except Exception as e:
                logger.warning(f"Pfadpruefung {path} fehlgeschlagen: {e}")
        return found

    def _security_headers(self, headers):
        present, missing = {}, []
        for h in SECURITY_HEADERS:
            value = headers.get(h)
            if value:
                present[h] = value[:120]
            else:
                missing.append(h)
        return {'present': present, 'missing': missing}

    def _is_valid_email(self, email):
        return bool(email) and '@' in email and '.' in email.split('@')[-1]

    # =====================
    # BEFUNDE OHNE MODELL
    # =====================

    def _deterministic_findings(self, data):
        """Pruefbare Befunde ohne Modellaufruf. Basis fuer Gratis-Scan und Prompt."""
        f = []

        if not data['has_ssl']:
            f.append(('hoch', 'Seite wird unverschlüsselt ausgeliefert', data['url']))
        if data.get('tls_error'):
            f.append(('hoch', 'Zertifikat ist ungültig oder nicht vertrauenswürdig', data['url']))

        for item in data.get('exposed_paths', []):
            f.append(('hoch', 'Interne Datei ist öffentlich abrufbar', item['path']))

        if data.get('mixed_content'):
            f.append(('mittel', 'Unverschlüsselte Inhalte auf verschlüsselter Seite',
                      data['mixed_content'][0]))

        for h in data['security_headers']['missing']:
            stufe = 'mittel' if h in ('Content-Security-Policy', 'Strict-Transport-Security') else 'niedrig'
            f.append((stufe, f'Sicherheitsangabe fehlt: {h}', data['origin']))

        for p in data.get('pages', []):
            if p['status'] >= 400:
                f.append(('mittel', f"Seite antwortet mit Fehler {p['status']}", p['url']))
            if not p['title']:
                f.append(('niedrig', 'Seitentitel fehlt', p['url']))
            if not p['has_meta_description']:
                f.append(('niedrig', 'Kurzbeschreibung fehlt', p['url']))
            if not p['has_viewport']:
                f.append(('mittel', 'Seite ist nicht für Mobilgeräte eingerichtet', p['url']))
            if p['bytes'] > 2_000_000:
                f.append(('mittel', 'Seite ist sehr groß und lädt langsam', p['url']))
            if p['images_without_alt'] > 0:
                f.append(('niedrig',
                          f"{p['images_without_alt']} Bilder ohne Alternativtext", p['url']))

        if not data.get('robots_txt'):
            f.append(('niedrig', 'robots.txt fehlt', data['origin'] + '/robots.txt'))

        rang = {'hoch': 0, 'mittel': 1, 'niedrig': 2}
        f.sort(key=lambda x: rang[x[0]])
        return f

    def _score(self, findings):
        punkte = 100
        for stufe, _, _ in findings:
            punkte -= {'hoch': 18, 'mittel': 7, 'niedrig': 2}[stufe]
        punkte = max(0, punkte)
        note = 'A' if punkte >= 90 else 'B' if punkte >= 75 else 'C' if punkte >= 60 else 'D' if punkte >= 40 else 'F'
        return punkte, note

    # =====================
    # GRATIS-SCAN
    # =====================

    def quick_scan(self, url, sichtbar=3):
        """Kostenloser Vorab-Scan. Nur Startseite, kein Modellaufruf, keine API-Kosten."""
        url = self._normalize_url(url)
        data = self._collect_website_data(url, max_pages=1, timeout=12)
        if not data:
            return None

        findings = self._deterministic_findings(data)
        punkte, note = self._score(findings)

        return {
            'host': urlparse(data['url']).netloc,
            'score': note,
            'punkte': punkte,
            'seiten': data['pages_crawled'],
            'gefunden': len(findings),
            'offen': [
                {'stufe': s.upper(), 'klasse': s, 'titel': t, 'wo': w}
                for s, t, w in findings[:sichtbar]
            ],
        }

    # =====================
    # ANALYSE
    # =====================

    def _analyze_with_claude(self, website_url, data, package='standard'):
        try:
            findings = self._deterministic_findings(data)
            punkte, _ = self._score(findings)
            befundliste = "\n".join(f"- [{s}] {t} — {w}" for s, t, w in findings[:60])

            seiten = "\n".join(
                f"- {p['url']} | Status {p['status']} | {p['bytes']} Bytes | {p['load_ms']} ms | Titel: {p['title'][:60]!r}"
                for p in data['pages'][:40]
            )

            prompt = f"""You are auditing a website for a paying client. Be specific and factual.
Base every statement on the measured data below. Do not invent findings.

WEBSITE: {website_url}
PACKAGE: {package}
PAGES CRAWLED: {data['pages_crawled']}
AVERAGE LOAD TIME: {data['avg_load_ms']} ms
TOTAL TRANSFERRED: {data['total_bytes']} bytes
TLS: {'valid' if data['has_ssl'] and not data['tls_error'] else 'PROBLEM: ' + str(data['tls_error'] or 'no HTTPS')}

SECURITY HEADERS PRESENT: {json.dumps(data['security_headers']['present'], ensure_ascii=False)}
SECURITY HEADERS MISSING: {', '.join(data['security_headers']['missing']) or 'none'}
PUBLICLY EXPOSED FILES: {json.dumps(data['exposed_paths'], ensure_ascii=False) or 'none'}
MIXED CONTENT SAMPLES: {json.dumps(data['mixed_content'][:5], ensure_ascii=False) or 'none'}

CRAWLED PAGES:
{seiten}

DETERMINISTIC FINDINGS (already verified, rank and explain these):
{befundliste or 'none'}

COMPUTED BASE SCORE: {punkte}/100

HOMEPAGE HTML (first 12KB):
{data['html_preview'][:12000]}

Write the report in German. Every issue must name the exact URL or header it refers to.
Return ONLY valid JSON, no markdown fences, with exactly this structure:
{{
  "summary": "1-2 Saetze Gesamturteil",
  "score": 0-100,
  "critical_issues": [{{"title": "...", "description": "...", "fix": "..."}}],
  "warnings": [{{"title": "...", "description": "...", "fix": "..."}}],
  "good_practices": [{{"title": "...", "description": "..."}}],
  "top_5_recommendations": ["1. ...", "2. ...", "3. ...", "4. ...", "5. ..."]
}}"""

            message = self.client.messages.create(
                model=MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )

            text = message.content[0].text.strip()
            if text.startswith('```'):
                text = text.split('```')[1]
                if text.startswith('json'):
                    text = text[4:]
                text = text.strip()

            result = json.loads(text)
            result.setdefault('score', punkte)
            result['_package'] = package
            result['_pages_crawled'] = data['pages_crawled']
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Antwort war kein gueltiges JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Analyse fehlgeschlagen: {e}", exc_info=True)
            return None

    # =====================
    # PDF GENERATION
    # =====================
    
    def _generate_pdf_report(self, website_url, audit_result):
        """Generate PDF report from audit result"""
        try:
            buffer = io.BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
            
            story = []
            styles = getSampleStyleSheet()
            
            # Custom styles
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=24,
                textColor=colors.HexColor('#1e3a8a'),
                spaceAfter=6,
                fontName='Helvetica-Bold'
            )
            
            heading_style = ParagraphStyle(
                'CustomHeading',
                parent=styles['Heading2'],
                fontSize=14,
                textColor=colors.HexColor('#1e40af'),
                spaceAfter=12,
                spaceBefore=12,
                fontName='Helvetica-Bold'
            )
            
            normal_style = ParagraphStyle(
                'CustomNormal',
                parent=styles['Normal'],
                fontSize=10,
                textColor=colors.HexColor('#374151'),
                spaceAfter=6,
                alignment=TA_JUSTIFY
            )
            
            # Header
            story.append(Paragraph("Website Security & Performance Audit", title_style))
            story.append(Paragraph(f"Report for: {website_url}", styles['Normal']))
            story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}", styles['Normal']))
            story.append(Paragraph("By JARVIS Security", styles['Normal']))
            story.append(Spacer(1, 0.3*inch))
            
            # Executive Summary
            score = audit_result.get('score', 0)
            score_color = '#dc2626' if score < 40 else '#f59e0b' if score < 70 else '#16a34a'
            
            story.append(Paragraph("Executive Summary", heading_style))
            story.append(Paragraph(audit_result.get('summary', 'Analysis complete.'), normal_style))
            story.append(Paragraph(f"<b>Security Score: <font color=\"{score_color}\">{score}/100</font></b>", normal_style))
            story.append(Spacer(1, 0.2*inch))
            
            # Critical Issues
            critical = audit_result.get('critical_issues', [])
            if critical:
                story.append(Paragraph("🔴 Critical Issues", heading_style))
                for issue in critical:
                    story.append(Paragraph(f"<b>{issue.get('title', 'Issue')}</b>", styles['Normal']))
                    story.append(Paragraph(f"{issue.get('description', '')}", normal_style))
                    story.append(Paragraph(f"<b>Fix:</b> {issue.get('fix', '')}", normal_style))
                    story.append(Spacer(1, 0.1*inch))
            
            # Warnings
            warnings = audit_result.get('warnings', [])
            if warnings:
                story.append(Paragraph("🟡 Warnings", heading_style))
                for warning in warnings:
                    story.append(Paragraph(f"<b>{warning.get('title', 'Warning')}</b>", styles['Normal']))
                    story.append(Paragraph(f"{warning.get('description', '')}", normal_style))
                    story.append(Spacer(1, 0.1*inch))
            
            # Good Practices
            good = audit_result.get('good_practices', [])
            if good:
                story.append(Paragraph("🟢 Good Practices", heading_style))
                for practice in good[:6]:
                    story.append(Paragraph(f"✓ {practice.get('title', 'Good practice')}", normal_style))
            
            # Top Recommendations
            story.append(Spacer(1, 0.2*inch))
            story.append(Paragraph("Top Recommendations", heading_style))
            for rec in audit_result.get('top_5_recommendations', [])[:5]:
                story.append(Paragraph(rec, normal_style))
            
            # Footer
            story.append(Spacer(1, 0.3*inch))
            story.append(Paragraph("This report is automated and generated by JARVIS Security. For detailed advice, consult a security professional.", styles['Normal']))
            
            # Build PDF
            doc.build(story)
            buffer.seek(0)
            return buffer.getvalue()
            
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            return None
    
    # =====================
    # EMAIL SENDING
    # =====================
    
    def _send_audit_email(self, to_email, website_url, pdf_bytes):
        """Send audit report via SendGrid"""
        try:
            import base64
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
            
            sg = SendGridAPIClient(self.sendgrid_api_key)
            
            subject = f"Website Audit Report: {self._extract_domain(website_url)}"
            
            html_content = f"""
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
<h2 style="color: #1e3a8a;">Your Website Audit is Ready</h2>
<p>Hi there,</p>
<p>Your security and performance audit for <strong>{website_url}</strong> is complete.</p>
<p>Please find the detailed report attached as a PDF.</p>
<p><strong>Next Steps:</strong></p>
<ul>
<li>Review the critical issues first</li>
<li>Address warnings within 30 days</li>
<li>Maintain good practices</li>
</ul>
<p>Questions? Reply to this email or visit our website.</p>
<p>Best regards,<br><strong>JARVIS Security Team</strong></p>
<hr style="border: none; border-top: 1px solid #ddd; margin: 20px 0;">
<p style="font-size: 12px; color: #999;">This report was automatically generated. For professional security consulting, please contact us directly.</p>
</body>
</html>
"""
            
            message = Mail(
                from_email=self.from_email,
                to_emails=to_email,
                subject=subject,
                html_content=html_content
            )
            
            # Attach PDF
            if pdf_bytes:
                encoded_pdf = base64.b64encode(pdf_bytes).decode()
                attachment = Attachment(
                    FileContent(encoded_pdf),
                    FileName(f"audit-{self._extract_domain(website_url)}.pdf"),
                    FileType("application/pdf"),
                    Disposition("attachment")
                )
                message.attachment = attachment
            
            response = sg.send(message)
            logger.info(f"Email sent to {to_email}, status: {response.status_code}")
            return True
            
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False
    
    def _send_error_email(self, to_email, website_url, error_msg):
        """Send error notification"""
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail
            
            sg = SendGridAPIClient(self.sendgrid_api_key)
            
            html_content = f"""
<html>
<body style="font-family: Arial, sans-serif;">
<h2 style="color: #dc2626;">Audit Could Not Complete</h2>
<p>We encountered an issue while auditing <strong>{website_url}</strong>:</p>
<p style="background: #fee2e2; padding: 10px; border-left: 4px solid #dc2626;">
{error_msg}
</p>
<p>Please check that your website is accessible and try again.</p>
<p>JARVIS Security Team</p>
</body>
</html>
"""
            
            message = Mail(
                from_email=self.from_email,
                to_emails=to_email,
                subject=f"Audit Failed: {website_url}",
                html_content=html_content
            )
            
            sg.send(message)
            logger.info(f"Error email sent to {to_email}")
            
        except Exception as e:
            logger.error(f"Error email failed: {e}")
    
    def _extract_domain(self, url):
        """Extract domain from URL"""
        try:
            parsed = urlparse(url)
            return parsed.netloc.replace('www.', '')
        except:
            return 'website'
