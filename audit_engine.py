import requests
import json
import logging
import smtplib
import urllib3
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import io
from anthropic import Anthropic

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.lib import colors
from urllib.parse import urlparse
import ssl

logger = logging.getLogger(__name__)

class WebsiteAuditBot:
    def __init__(self, anthropic_api_key, sendgrid_api_key, from_email):
        self.client = Anthropic(api_key=anthropic_api_key)
        self.anthropic_api_key = anthropic_api_key
        self.sendgrid_api_key = sendgrid_api_key
        self.from_email = from_email
    
    def run_audit(self, website_url, customer_email, payment_id):
        """Main audit orchestration"""
        try:
            logger.info(f"Starting audit for {website_url} ({payment_id})")
            
            # Validate email
            if not self._is_valid_email(customer_email):
                logger.error(f"Invalid email: {customer_email}")
                return
            
            # Normalize URL
            website_url = self._normalize_url(website_url)
            
            # Collect website data
            logger.info(f"Collecting data from {website_url}")
            website_data = self._collect_website_data(website_url)
            
            if not website_data:
                self._send_error_email(customer_email, website_url, "Could not reach website")
                return
            
            # Analyze with Claude
            logger.info("Analyzing with Claude API")
            audit_result = self._analyze_with_claude(website_url, website_data)
            
            if not audit_result:
                self._send_error_email(customer_email, website_url, "Analysis failed")
                return
            
            # Generate PDF
            logger.info("Generating PDF report")
            pdf_bytes = self._generate_pdf_report(website_url, audit_result)
            
            # Send email with attachment
            logger.info(f"Sending report to {customer_email}")
            self._send_audit_email(customer_email, website_url, pdf_bytes)
            
            logger.info(f"Audit completed successfully: {payment_id}")
            
        except Exception as e:
            logger.error(f"Audit failed: {e}", exc_info=True)
            try:
                self._send_error_email(customer_email, website_url, str(e))
            except:
                pass
    
    # =====================
    # DATA COLLECTION
    # =====================
    
    def _normalize_url(self, url):
        """Ensure URL has protocol"""
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url.rstrip('/')
    
    def _collect_website_data(self, url, timeout=15):
        """Crawl website and collect audit data"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Website Audit Bot)'
            }
            
            # Disable SSL verification for self-signed certs
            response = requests.get(url, headers=headers, timeout=timeout, verify=False)
            response.raise_for_status()
            
            html = response.text
            
            # Extract key metrics
            data = {
                'url': url,
                'status_code': response.status_code,
                'headers': dict(response.headers),
                'html_size': len(html),
                'has_ssl': url.startswith('https'),
                'has_mobile_viewport': 'viewport' in html.lower(),
                'has_title': '<title>' in html.lower(),
                'has_meta_description': 'meta' in html.lower() and 'description' in html.lower(),
                'has_favicon': 'favicon' in html.lower() or 'icon' in html.lower(),
                'has_robots_txt': self._check_robots_txt(url),
                'html_preview': html[:5000],  # First 5KB for Claude
            }
            
            return data
            
        except requests.exceptions.Timeout:
            logger.error(f"Timeout accessing {url}")
            return None
        except requests.exceptions.SSLError:
            logger.warning(f"SSL error for {url}, retrying without verification")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            return None
    
    def _check_robots_txt(self, url):
        """Check if robots.txt exists"""
        try:
            base_url = '/'.join(url.split('/')[:3])
            response = requests.get(f"{base_url}/robots.txt", timeout=5, verify=False)
            return response.status_code == 200
        except:
            return False
    
    def _is_valid_email(self, email):
        """Basic email validation"""
        return '@' in email and '.' in email.split('@')[1]
    
    # =====================
    # CLAUDE ANALYSIS
    # =====================
    
    def _analyze_with_claude(self, website_url, website_data):
        """Send data to Claude for analysis"""
        try:
            prompt = f"""
Analyze this website audit data and provide security and performance recommendations.

WEBSITE: {website_url}
STATUS CODE: {website_data['status_code']}
SSL ENABLED: {website_data['has_ssl']}
PAGE SIZE: {website_data['html_size']} bytes
MOBILE VIEWPORT: {website_data['has_mobile_viewport']}
HAS TITLE TAG: {website_data['has_title']}
HAS META DESCRIPTION: {website_data['has_meta_description']}
HAS FAVICON: {website_data['has_favicon']}
HAS ROBOTS.TXT: {website_data['has_robots_txt']}

SECURITY HEADERS CHECK:
{self._check_security_headers(website_data['headers'])}

HTML PREVIEW (first 5KB):
{website_data['html_preview'][:2000]}

Please provide a structured analysis with:
1. Critical Issues (🔴) - Fix immediately
2. Warnings (🟡) - Fix soon
3. Good Practices (🟢) - Keep it up

Format as JSON with this structure:
{{
  "summary": "1-2 sentence executive summary",
  "score": 0-100,
  "critical_issues": [
    {{"title": "Issue name", "description": "Description", "fix": "How to fix"}}
  ],
  "warnings": [
    {{"title": "...", "description": "...", "fix": "..."}}
  ],
  "good_practices": [
    {{"title": "...", "description": "..."}}
  ],
  "top_5_recommendations": [
    "1. ...",
    "2. ...",
    "3. ...",
    "4. ...",
    "5. ..."
  ]
}}

Return ONLY valid JSON, no markdown formatting.
"""
            
            message = self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=2000,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            response_text = message.content[0].text
            
            # Parse JSON
            audit_data = json.loads(response_text)
            return audit_data
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return None
    
    def _check_security_headers(self, headers):
        """Analyze security headers"""
        important_headers = [
            'Content-Security-Policy',
            'Strict-Transport-Security',
            'X-Content-Type-Options',
            'X-Frame-Options',
            'X-XSS-Protection'
        ]
        
        found = []
        missing = []
        
        for header in important_headers:
            if header in headers:
                found.append(f"✓ {header}: {headers[header][:50]}")
            else:
                missing.append(f"✗ {header}: Missing")
        
        return "\n".join(found + missing)
    
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
                for warning in warnings[:3]:  # Limit to 3
                    story.append(Paragraph(f"<b>{warning.get('title', 'Warning')}</b>", styles['Normal']))
                    story.append(Paragraph(f"{warning.get('description', '')}", normal_style))
                    story.append(Spacer(1, 0.1*inch))
            
            # Good Practices
            good = audit_result.get('good_practices', [])
            if good:
                story.append(Paragraph("🟢 Good Practices", heading_style))
                for practice in good[:3]:  # Limit to 3
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
