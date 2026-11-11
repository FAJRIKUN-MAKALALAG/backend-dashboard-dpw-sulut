from app.routes.auth_routes import bp as auth_bp
from app.routes.members import bp as members_bp
from app.routes.audit import bp as audit_bp
from app.routes.deletions import bp as deletions_bp
from app.routes.users import bp as users_bp
from app.routes.stats import bp as stats_bp
from app.routes.ai import bp as ai_bp
from app.routes.referrals import bp as referrals_bp
from app.routes.upload import bp as upload_bp
from app.routes.structure_slots import bp as structure_slots_bp
from app.ktp_scanner import ktp_scanner_bp
from app.dpp_integration import dpp_bp


def register_blueprints(app):
    for bp in (
        auth_bp,
        members_bp,
        audit_bp,
        deletions_bp,
        users_bp,
        stats_bp,
        ai_bp,
        referrals_bp,
        upload_bp,
        structure_slots_bp,
        ktp_scanner_bp,
        dpp_bp,
    ):
        app.register_blueprint(bp, url_prefix="/api")
