import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import stripe
from dotenv import load_dotenv
from audit_engine import WebsiteAuditBot
import threading

load_dotenv()

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Setup Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')

# Initialize bot
bot = WebsiteAuditBot(
    anthropic_api_key=os.getenv('ANTHROPIC_API_KEY'),
    sendgrid_api_key=os.getenv('SENDGRID_API_KEY'),
    from_email=os.getenv('FROM_EMAIL', 'audits@jarvis-security.bot')
)

# =====================
# ROUTES
# =====================

@app.route('/', methods=['GET'])
def index():
    """Serve landing page"""
    # Try multiple paths for static files
    paths_to_try = ['/app/static/index.html', 'static/index.html', './static/index.html']
    for path in paths_to_try:
        if os.path.exists(path):
            try:
                return open(path).read()
            except Exception as e:
                logger.warning(f"Could not read {path}: {e}")
    
    # Fallback landing page if static file not found
    return '''<!DOCTYPE html>
    <html>
    <head>
        <title>Website Security Audit</title>
        <style>
            body { font-family: Arial; text-align: center; padding: 50px; }
            .container { max-width: 600px; margin: 0 auto; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Website Security & Performance Audit</h1>
            <p>Service temporarily unavailable. Please try again later.</p>
        </div>
    </body>
    </html>'''

@app.route('/config', methods=['GET'])
def get_config():
    """Return Stripe publishable key for frontend"""
    return jsonify({
        'publishableKey': os.getenv('STRIPE_PUBLISHABLE_KEY')
    })

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
        logger.info(f"Webhook event received: {event['type']}")
    except ValueError as e:
        logger.error(f"Invalid payload: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid signature: {e}")
        return jsonify({'error': 'Invalid signature'}), 400
    
    # Handle payment_intent.succeeded
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        metadata = payment_intent.get('metadata', {})
        
        website_url = metadata.get('website_url')
        customer_email = metadata.get('customer_email')
        payment_id = payment_intent['id']
        
        logger.info(f"Payment succeeded for {customer_email}: {website_url}")
        
        # Start audit in background thread (doesn't block webhook response)
        thread = threading.Thread(
            target=bot.run_audit,
            args=(website_url, customer_email, payment_id),
            daemon=True
        )
        thread.start()
        
        return jsonify({'received': True}), 200
    
    return jsonify({'received': True}), 200

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    """Create Stripe checkout session"""
    try:
        data = request.json
        website_url = data.get('website_url')
        customer_email = data.get('customer_email')
        amount = data.get('amount', 20000)  # Default €200
        
        if not website_url or not customer_email:
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Create Stripe checkout session
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': 'eur',
                        'unit_amount': amount,  # in cents
                        'product_data': {
                            'name': 'Website Security & Performance Audit',
                            'description': f'Audit for {website_url}',
                        },
                    },
                    'quantity': 1,
                }
            ],
            metadata={
                'website_url': website_url,
                'customer_email': customer_email,
            },
            customer_email=customer_email,
            mode='payment',
            success_url=os.getenv('DOMAIN', 'http://localhost:5000') + '/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=os.getenv('DOMAIN', 'http://localhost:5000') + '/?cancelled=1',
        )
        
        logger.info(f"Checkout session created: {session.id} for {customer_email}")
        
        return jsonify({
            'url': session.url,
            'session_id': session.id
        }), 200
        
    except stripe.error.StripeInvalidRequestError as e:
        logger.error(f"Invalid Stripe request: {e}")
        return jsonify({'error': 'Invalid request'}), 400
    except Exception as e:
        logger.error(f"Checkout session creation failed: {e}")
        return jsonify({'error': 'Failed to create payment session'}), 500

@app.route('/success', methods=['GET'])
def success():
    """Payment success page"""
    session_id = request.args.get('session_id')
    cancelled = request.args.get('cancelled')
    
    if cancelled:
        return '''
        <html>
            <body style="font-family: Arial; text-align: center; padding: 50px;">
                <h1>Payment Cancelled</h1>
                <p>No charges were made.</p>
                <a href="/">← Back to Audit</a>
            </body>
        </html>
        '''
    
    return '''
    <html>
        <body style="font-family: Arial; text-align: center; padding: 50px;">
            <h1>✅ Payment Successful!</h1>
            <p>Your audit is starting now.</p>
            <p>You'll receive your report via email within 2-4 hours.</p>
            <a href="/">← Start Another Audit</a>
        </body>
    </html>
    '''

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({'status': 'healthy'}), 200

# =====================
# ERROR HANDLERS
# =====================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({'error': 'Internal server error'}), 500

# =====================
# MAIN
# =====================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=os.getenv('FLASK_ENV') == 'development'
    )
