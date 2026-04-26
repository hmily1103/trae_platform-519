from flask import Blueprint

remote_control_bp = Blueprint('remote_control', __name__, template_folder='templates')

from . import views
