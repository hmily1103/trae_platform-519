from flask import Blueprint, render_template, jsonify

stb_calculator_bp = Blueprint('stb_calculator', __name__, template_folder='templates')

@stb_calculator_bp.route('/')
def index():
    return render_template('stb_calculator_index.html')

@stb_calculator_bp.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({'ok': True, 'state': 'idle'})
