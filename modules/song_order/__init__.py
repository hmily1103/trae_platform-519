from flask import Blueprint

song_order_bp = Blueprint('song_order', __name__, template_folder='templates')

from . import views
