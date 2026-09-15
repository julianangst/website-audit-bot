from flask import Flask
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

logger.info("=" * 50)
logger.info("✓ Flask app started successfully!")
logger.info("=" * 50)

@app.route('/', methods=['GET'])
def index():
    return {
        'status': 'healthy',
        'service': 'Website Audit Bot',
        'version': '1.0'
    }

@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok'}, 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    logger.info(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
