from flask import Blueprint

server_stress_bp = Blueprint('server_stress', __name__, template_folder='templates')

from . import views
