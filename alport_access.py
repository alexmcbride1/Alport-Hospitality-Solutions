"""Keep verified company-preview access scoped to its existing business user."""

def preview_session_values(session, user):
    if not user or session.get('billing_preview_admin') is not True:
        return {}
    try:
        uid, oid = int(user['id']), int(user['organisation_id'])
        if int(session.get('user_id') or 0) != uid:
            return {}
        if int(session.get('billing_preview_bypass_org') or 0) != oid:
            return {}
        # Older signed sessions have no explicit user binding. Only upgrade them
        # while the same authenticated business user remains in that session.
        if int(session.get('billing_preview_user_id', uid)) != uid:
            return {}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    return dict(billing_preview_admin=True, billing_preview_bypass_org=oid,
                billing_preview_user_id=uid)
