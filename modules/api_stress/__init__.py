from flask import Blueprint

api_stress_bp = Blueprint('api_stress', __name__, template_folder='templates')

from . import views
