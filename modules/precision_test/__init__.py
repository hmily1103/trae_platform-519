from flask import Blueprint

precision_test_bp = Blueprint(
    'precision_test',
    __name__,
    template_folder='templates',
    url_prefix='/precision_test'
)

from . import views
