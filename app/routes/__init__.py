from .auth_routes import bp as auth_bp
from .members import bp as members_bp
from .audit import bp as audit_bp
from .deletions import bp as deletions_bp
from .users import bp as users_bp
from .stats import bp as stats_bp
from .ai import bp as ai_bp
from .referrals import bp as referrals_bp


def register_blueprints(app):
    for bp in (auth_bp, members_bp, audit_bp, deletions_bp, users_bp, stats_bp, ai_bp, referrals_bp):
        app.register_blueprint(bp, url_prefix="/api")
