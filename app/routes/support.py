import uuid
from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app.auth import current_claims, login_required, super_admin_required
from app.extensions import db
from app.models import SupportMessage

bp = Blueprint("support", __name__)


def _get_recipient_name(rec_nik: str) -> str:
    if not rec_nik or rec_nik == "ALL_DPW":
        return "Tim Admin DPW Standby"
    if rec_nik in ("7106033003050001", "master-superadmin-01", "SUPERADMINDPW"):
        return "Super Administrator Utama DPW"
    return f"Admin DPW ({rec_nik})"


def _msg_to_dict(m: SupportMessage) -> dict:
    rec_nik = m.recipient_nik
    rec_name = getattr(m, "recipient_name", None) or _get_recipient_name(rec_nik)
    return {
        "id": m.id,
        "threadId": m.thread_id,
        "senderNik": m.sender_nik,
        "senderName": m.sender_name,
        "senderRole": m.sender_role,
        "senderDpd": m.sender_dpd,
        "senderDpc": m.sender_dpc,
        "senderDprt": m.sender_dprt,
        "senderJabatan": m.sender_jabatan,
        "recipientRole": m.recipient_role,
        "recipientNik": rec_nik or "ALL_DPW",
        "recipientName": rec_name,
        "category": m.category or "UMUM",
        "message": m.message,
        "attachmentUrl": m.attachment_url,
        "isRead": bool(m.is_read),
        "createdAt": m.created_at.isoformat() if m.created_at else None,
    }


@bp.get("/support/admins")
@login_required
def get_available_support_admins():
    """Mengembalikan daftar Super Admin & Admin DPW yang dapat dihubungi pengurus."""
    from app.models import AppUser

    rows = (
        AppUser.query.filter(
            AppUser.role.in_(["SUPER_ADMIN", "ADMIN_DPW"]),
            AppUser.is_active == True,
        )
        .order_by(AppUser.role.asc(), AppUser.full_name.asc())
        .all()
    )
    items = []
    seen = set()
    for u in rows:
        admin_id = u.member_nik or u.username or str(u.id)
        if admin_id in seen:
            continue
        seen.add(admin_id)
        items.append(
            {
                "id": admin_id,
                "nik": u.member_nik or u.username,
                "name": u.full_name or u.username,
                "role": u.role,
                "jabatan": u.jabatan or ("Super Administrator DPW" if u.role == "SUPER_ADMIN" else "Admin DPW"),
            }
        )

    # Selalu pastikan Super Administrator Utama DPW ada dalam daftar opsi
    has_super = any(it["role"] == "SUPER_ADMIN" for it in items)
    if not has_super:
        items.insert(
            0,
            {
                "id": "7106033003050001",
                "nik": "7106033003050001",
                "name": "Super Administrator Utama DPW",
                "role": "SUPER_ADMIN",
                "jabatan": "Super Administrator DPW Partai NasDem Sulut",
            },
        )

    return jsonify(items)


@bp.get("/support/messages")
@login_required
def get_messages():
    claims = current_claims()
    role = claims.get("role")
    user_nik = claims.get("adminNik") or claims.get("member_nik") or claims.get("sub") or "USER"

    req_thread = request.args.get("thread_id")

    if role == "SUPER_ADMIN":
        target_thread = req_thread or f"{user_nik}__ALL_DPW"
    else:
        # Pengurus membaca thread percakapan miliknya
        target_thread = req_thread or f"{user_nik}__ALL_DPW"

    # Jika membaca thread default ALL_DPW, sertakan juga fallback legacy thread {user_nik}
    if "__ALL_DPW" in target_thread:
        base_nik = target_thread.split("__ALL_DPW")[0]
        rows = (
            SupportMessage.query.filter(
                SupportMessage.thread_id.in_([target_thread, base_nik])
            )
            .order_by(SupportMessage.created_at.asc())
            .all()
        )
    else:
        rows = (
            SupportMessage.query.filter_by(thread_id=target_thread)
            .order_by(SupportMessage.created_at.asc())
            .all()
        )
    return jsonify([_msg_to_dict(r) for r in rows])


@bp.post("/support/messages")
@login_required
def send_message():
    claims = current_claims()
    role = claims.get("role")
    user_nik = claims.get("adminNik") or claims.get("member_nik") or claims.get("sub") or "USER"
    user_name = claims.get("adminName") or claims.get("full_name") or "Pengurus"
    user_dpd = claims.get("dpdAccess") or claims.get("dpd_access") or ""
    user_dpc = claims.get("dpcAccess") or claims.get("dpc_access") or ""
    user_dprt = claims.get("dprtAccess") or claims.get("dprt_access") or ""
    user_jabatan = claims.get("jabatan") or role

    data = request.get_json(silent=True) or {}
    msg_text = (data.get("message") or "").strip()
    if not msg_text:
        return jsonify(error="Pesan tidak boleh kosong"), 400

    msg_id = data.get("id") or f"msg_{uuid.uuid4().hex[:12]}"
    category = data.get("category") or "UMUM"

    if role == "SUPER_ADMIN":
        target_thread = data.get("threadId") or f"{user_nik}__ALL_DPW"
        recipient_role = "PENGURUS"
        recipient_nik = data.get("recipientNik")
    else:
        # Pengurus mengirim ke Super Admin / Admin DPW tertentu (setiap admin punya room tersendiri)
        raw_rec = str(data.get("recipientNik") or "").strip()
        recipient_nik = raw_rec if (raw_rec and raw_rec != "ALL_DPW") else "ALL_DPW"
        target_thread = data.get("threadId") or f"{user_nik}__{recipient_nik}"
        recipient_role = "SUPER_ADMIN"

    msg = SupportMessage(
        id=msg_id,
        thread_id=target_thread,
        sender_nik=user_nik,
        sender_name=user_name,
        sender_role=role,
        sender_dpd=user_dpd,
        sender_dpc=user_dpc,
        sender_dprt=user_dprt,
        sender_jabatan=user_jabatan,
        recipient_role=recipient_role,
        recipient_nik=recipient_nik,
        category=category,
        message=msg_text,
        attachment_url=data.get("attachmentUrl"),
        is_read=False,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify(_msg_to_dict(msg)), 201


@bp.get("/support/threads")
@super_admin_required
def get_threads():
    # Ambil seluruh thread percakapan untuk Super Admin
    all_msgs = (
        SupportMessage.query.order_by(SupportMessage.created_at.desc())
        .all()
    )

    thread_map = {}
    for m in all_msgs:
        t_id = m.thread_id
        if t_id not in thread_map:
            thread_map[t_id] = []
        thread_map[t_id].append(m)

    threads = []
    for t_id, msgs in thread_map.items():
        last_m = msgs[0]
        # Cari pesan pengurus untuk informasi profil
        pengurus_m = next((m for m in msgs if m.sender_role != "SUPER_ADMIN"), last_m)
        unread_count = sum(
            1 for m in msgs if m.recipient_role == "SUPER_ADMIN" and not m.is_read
        )

        rec_nik = last_m.recipient_nik
        rec_name = getattr(last_m, "recipient_name", None) or _get_recipient_name(rec_nik)

        threads.append(
            {
                "threadId": t_id,
                "pengurusNik": pengurus_m.sender_nik,
                "pengurusName": pengurus_m.sender_name,
                "pengurusRole": pengurus_m.sender_role,
                "pengurusDpd": pengurus_m.sender_dpd or "Sulawesi Utara",
                "pengurusDpc": pengurus_m.sender_dpc,
                "pengurusDprt": pengurus_m.sender_dprt,
                "pengurusJabatan": pengurus_m.sender_jabatan,
                "lastMessage": last_m.message,
                "lastMessageTime": last_m.created_at.isoformat() if last_m.created_at else None,
                "category": last_m.category or "UMUM",
                "recipientNik": rec_nik or "ALL_DPW",
                "recipientName": rec_name,
                "unreadCount": unread_count,
            }
        )

    threads.sort(key=lambda x: x["lastMessageTime"] or "", reverse=True)
    return jsonify(threads)


@bp.post("/support/read")
@login_required
def mark_read():
    claims = current_claims()
    role = claims.get("role")
    data = request.get_json(silent=True) or {}
    thread_id = data.get("thread_id")
    if not thread_id:
        return jsonify(error="thread_id wajib diisi"), 400

    if role == "SUPER_ADMIN":
        SupportMessage.query.filter_by(
            thread_id=thread_id, recipient_role="SUPER_ADMIN", is_read=False
        ).update({"is_read": True})
    else:
        SupportMessage.query.filter_by(
            thread_id=thread_id, recipient_role="PENGURUS", is_read=False
        ).update({"is_read": True})

    db.session.commit()
    return jsonify(success=True)


@bp.delete("/support/messages/<message_id>")
@super_admin_required
def delete_message(message_id: str):
    """Menghapus satu pesan tertentu (khusus Super Admin)."""
    msg = db.session.get(SupportMessage, message_id)
    if not msg:
        return jsonify(error="Pesan tidak ditemukan"), 404

    thread_id = msg.thread_id
    db.session.delete(msg)
    db.session.commit()
    return jsonify(success=True, messageId=message_id, threadId=thread_id), 200


@bp.delete("/support/threads")
@super_admin_required
def delete_thread():
    """Menghapus seluruh riwayat percakapan di suatu room chat (khusus Super Admin)."""
    data = request.get_json(silent=True) or {}
    thread_id = request.args.get("thread_id") or data.get("threadId") or data.get("thread_id")
    if not thread_id:
        return jsonify(error="Parameter thread_id wajib disertakan"), 400

    deleted_count = SupportMessage.query.filter_by(thread_id=thread_id).delete()
    if "__ALL_DPW" in thread_id:
        base_nik = thread_id.split("__ALL_DPW")[0]
        deleted_count += SupportMessage.query.filter_by(thread_id=base_nik).delete()

    db.session.commit()
    return jsonify(success=True, threadId=thread_id, deletedCount=deleted_count), 200

