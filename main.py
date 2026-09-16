import os
import logging
import threading
import time
from collections import deque

from flask import Flask, request, jsonify, send_from_directory
import stripe
from dotenv import load_dotenv

from audit_engine import WebsiteAuditBot

load_dotenv()

app = Flask(__name__, static_folder='static')
app.config['JSON_SORT_KEYS'] = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL')

# Preise gehoeren auf den Server. Was der Browser schickt, wird ignoriert.
PRICES = {
    'quick':    {'amount': 2500,  'name': 'Kurzbericht (10 Seiten)'},
    'standard': {'amount': 7500,  'name': 'Standardbericht (20 Seiten)'},
    'pro':      {'amount': 15000, 'name': 'Vollbericht (alle Seiten)'},
}
DEFAULT_PACKAGE = 'standard'

logger.info("=" * 50)
for key in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'ANTHROPIC_API_KEY', 'SENDGRID_API_KEY'):
    logger.info(f"{key}: {'gesetzt' if os.getenv(key) else 'FEHLT'}")
logger.info(f"FROM_EMAIL: {os.getenv('FROM_EMAIL', 'nicht gesetzt')}")
logger.info(f"ADMIN_EMAIL: {ADMIN_EMAIL or 'nicht gesetzt'}")
logger.info("=" * 50)

try:
    bot = WebsiteAuditBot(
        anthropic_api_key=os.getenv('ANTHROPIC_API_KEY'),
        sendgrid_api_key=os.getenv('SENDGRID_API_KEY'),
        from_email=os.getenv('FROM_EMAIL', 'audits@hamkoders.at'),
    )
    logger.info("Audit-Engine bereit")
except Exception as e:
    logger.error(f"Audit-Engine konnte nicht starten: {e}", exc_info=True)
    bot = None

# Bereits verarbeitete Zahlungen. Stripe stellt Webhooks mehrfach zu.
_verarbeitet = set()
_verarbeitet_lock = threading.Lock()

# Einfache Drossel fuer den Gratis-Scan, damit er nicht als fremder Scanner dient.
_scan_log = {}
_scan_lock = threading.Lock()
SCAN_LIMIT, SCAN_FENSTER = 5, 3600


def _schon_verarbeitet(kennung):
    with _verarbeitet_lock:
        if kennung in _verarbeitet:
            return True
        _verarbeitet.add(kennung)
        if len(_verarbeitet) > 5000:
            _verarbeitet.clear()
        return False


def _darf_scannen(ip):
    jetzt = time.time()
    with _scan_lock:
        eintraege = _scan_log.setdefault(ip, deque())
        while eintraege and jetzt - eintraege[0] > SCAN_FENSTER:
            eintraege.popleft()
        if len(eintraege) >= SCAN_LIMIT:
            return False
        eintraege.append(jetzt)
        return True


def _alarm(betreff, text):
    """Meldet Stoerungen an den Betreiber, damit kein Auftrag still verloren geht."""
    if not (ADMIN_EMAIL and bot):
        logger.error(f"ALARM (kein Versand moeglich): {betreff} — {text}")
        return
    try:
        bot._send_error_email(ADMIN_EMAIL, betreff, text)
    except Exception as e:
        logger.error(f"Alarm konnte nicht versendet werden: {e}")


def _audit_starten(website_url, customer_email, zahlung_id, package):
    if not bot:
        _alarm(zahlung_id, "Zahlung eingegangen, aber die Audit-Engine laeuft nicht.")
        return
    if not website_url or not customer_email:
        _alarm(zahlung_id, f"Zahlung ohne Zieldaten: url={website_url!r} email={customer_email!r}")
        return
    threading.Thread(
        target=bot.run_audit,
        args=(website_url, customer_email, zahlung_id, package),
        daemon=True,
    ).start()


# =====================
# SEITEN
# =====================

@app.route('/', methods=['GET'])
def index():
    try:
        return send_from_directory(app.static_folder, 'index.html')
    except Exception:
        logger.error("static/index.html nicht gefunden")
        return "<h1>Website-Check</h1><p>Die Seite wird gerade aktualisiert.</p>", 503


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({'publishableKey': os.getenv('STRIPE_PUBLISHABLE_KEY')})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'engine': bool(bot)}), 200


# =====================
# GRATIS-SCAN
# =====================

@app.route('/api/free-scan', methods=['POST'])
def free_scan():
    """Kostenloser Vorab-Scan der Startseite. Kein Modellaufruf, keine API-Kosten."""
    if not bot:
        return jsonify({'error': 'Der Check ist gerade nicht verfügbar.'}), 503

    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
    ip = ip.split(',')[0].strip()
    if not _darf_scannen(ip):
        return jsonify({'error': 'Zu viele Prüfungen. Bitte in einer Stunde erneut versuchen.'}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url or len(url) > 300:
        return jsonify({'error': 'Bitte geben Sie eine gültige Adresse ein.'}), 400

    try:
        ergebnis = bot.quick_scan(url)
    except Exception as e:
        logger.error(f"Gratis-Scan fehlgeschlagen fuer {url}: {e}")
        return jsonify({'error': 'Die Adresse konnte nicht geprüft werden.'}), 502

    if not ergebnis:
        return jsonify({'error': 'Die Adresse war nicht erreichbar.'}), 502

    logger.info(f"Gratis-Scan: {ergebnis['host']} — {ergebnis['gefunden']} Befunde, Note {ergebnis['score']}")
    return jsonify(ergebnis), 200


# =====================
# BEZAHLUNG
# =====================

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}
        website_url = (data.get('website_url') or '').strip()
        customer_email = (data.get('customer_email') or '').strip()
        package = data.get('package', DEFAULT_PACKAGE)

        if package not in PRICES:
            return jsonify({'error': 'Unbekanntes Paket'}), 400
        if not website_url or not customer_email:
            return jsonify({'error': 'Adresse und E-Mail werden benötigt'}), 400
        if '@' not in customer_email:
            return jsonify({'error': 'Bitte geben Sie eine gültige E-Mail an'}), 400

        preis = PRICES[package]
        metadaten = {
            'website_url': website_url,
            'customer_email': customer_email,
            'package': package,
        }

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'unit_amount': preis['amount'],
                    'product_data': {
                        'name': preis['name'],
                        'description': f"Website-Check für {website_url}",
                    },
                },
                'quantity': 1,
            }],
            metadata=metadaten,
            payment_intent_data={'metadata': metadaten},
            customer_email=customer_email,
            mode='payment',
            success_url=os.getenv('DOMAIN', 'http://localhost:5000') + '/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=os.getenv('DOMAIN', 'http://localhost:5000') + '/?abgebrochen=1',
        )

        logger.info(f"Checkout angelegt: {session.id} | {package} | {preis['amount']/100:.2f} EUR")
        return jsonify({'url': session.url, 'session_id': session.id}), 200

    except stripe.error.StripeError as e:
        logger.error(f"Stripe-Fehler: {e}")
        return jsonify({'error': 'Zahlung konnte nicht gestartet werden'}), 400
    except Exception as e:
        logger.error(f"Checkout fehlgeschlagen: {e}", exc_info=True)
        return jsonify({'error': 'Zahlung konnte nicht gestartet werden'}), 500


@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig = request.headers.get('stripe-signature')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({'error': 'Ungültige Daten'}), 400
    except stripe.error.SignatureVerificationError:
        logger.error("Webhook-Signatur stimmt nicht")
        return jsonify({'error': 'Ungültige Signatur'}), 400

    typ = event['type']
    logger.info(f"Webhook: {typ}")

    # Das ist das Ereignis, das zu Stripe Checkout gehoert.
    if typ == 'checkout.session.completed':
        session = event['data']['object']
        if session.get('payment_status') != 'paid':
            logger.info(f"Session {session['id']} noch nicht bezahlt")
            return jsonify({'received': True}), 200

        if _schon_verarbeitet(session['id']):
            logger.info(f"Session {session['id']} bereits verarbeitet")
            return jsonify({'received': True}), 200

        m = session.get('metadata') or {}
        _audit_starten(
            m.get('website_url'),
            m.get('customer_email') or session.get('customer_email'),
            session['id'],
            m.get('package', DEFAULT_PACKAGE),
        )
        return jsonify({'received': True}), 200

    # Rueckfall fuer Zahlungen ausserhalb von Checkout.
    if typ == 'payment_intent.succeeded':
        pi = event['data']['object']
        m = pi.get('metadata') or {}
        if not m.get('website_url'):
            logger.info(f"PaymentIntent {pi['id']} ohne Zieldaten — wird von checkout.session.completed erledigt")
            return jsonify({'received': True}), 200
        if _schon_verarbeitet(pi['id']):
            return jsonify({'received': True}), 200
        _audit_starten(m.get('website_url'), m.get('customer_email'), pi['id'],
                       m.get('package', DEFAULT_PACKAGE))
        return jsonify({'received': True}), 200

    return jsonify({'received': True}), 200


@app.route('/success', methods=['GET'])
def success():
    return '''<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<title>Bezahlt</title><style>body{font-family:system-ui,sans-serif;background:#10312F;color:#EDE9E0;
text-align:center;padding:14vh 1.5rem}a{color:#F2B705}</style></head><body>
<h1>Zahlung eingegangen</h1>
<p>Der Check läuft jetzt. Der Bericht kommt per E-Mail.</p>
<p><a href="/">Weitere Adresse prüfen</a></p></body></html>'''


@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Nicht gefunden'}), 404


@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Interner Fehler: {e}")
    return jsonify({'error': 'Interner Fehler'}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_ENV') == 'development')
