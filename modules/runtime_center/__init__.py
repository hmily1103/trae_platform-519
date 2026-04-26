from flask import Blueprint

runtime_center_bp = Blueprint('runtime_center', __name__, 
                            url_prefix='/runtime_center',
                            template_folder='templates')

from . import views
