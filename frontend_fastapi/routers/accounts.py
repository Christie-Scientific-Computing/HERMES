"""
Routers for accounts/: sign in/out, invite, create user directly, activate,
list users. Port of accounts/views.py (Django) -- see that file for the
exact behavior being matched. Makes zero backend_client calls (all
local-DB), same as the Django original.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from frontend_fastapi import auth, email_backend, security
from frontend_fastapi.database import get_db
from frontend_fastapi.deps import get_session, get_template_context, require_data_custodian
from frontend_fastapi.flash import flash
from frontend_fastapi.forms.accounts import ActivateForm, CreateUserForm, InviteUserForm, LoginForm
from frontend_fastapi.models import Session, User
from frontend_fastapi.settings import LOGIN_REDIRECT_URL
from frontend_fastapi.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts", tags=["accounts"])

_DUPLICATE_USERNAME_ERROR = "A user with that username already exists."


def _reject_duplicate_username(form, db: DBSession) -> bool:
    """True (and appends a field error) if form.username.data is already
    taken. Must be called AFTER form.validate() -- WTForms' Field.errors
    is an immutable empty tuple until validate() runs and replaces it with
    a real list, so appending to it any earlier raises AttributeError.

    A plain check-then-insert: cheap and correct in the overwhelmingly
    common case, but two concurrent submissions for the same username can
    both pass this check before either has committed. _add_user_or_reject
    below is what actually guarantees no duplicate row -- this is just
    what makes the ordinary case return a clean form error instead of
    reaching that path at all."""
    if form.username.data and db.query(User).filter_by(username=form.username.data).one_or_none() is not None:
        form.username.errors.append(_DUPLICATE_USERNAME_ERROR)
        return True
    return False


def _add_user_or_reject(db: DBSession, form, new_user: User) -> bool:
    """Adds+flushes new_user, converting a raced unique-constraint
    violation on username (see _reject_duplicate_username's docstring)
    into the same form error rather than an unhandled IntegrityError.

    Unlike _reject_duplicate_username, doesn't assume form.validate() has
    already run -- form.username.errors starts as an immutable tuple until
    validate() replaces it with a real list (see that function's
    docstring), and nothing enforces that every caller of this one
    validates first, so this normalizes it defensively rather than risking
    the same AttributeError."""
    db.add(new_user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if not isinstance(form.username.errors, list):
            form.username.errors = list(form.username.errors)
        form.username.errors.append(_DUPLICATE_USERNAME_ERROR)
        return False
    return True


def _safe_next(next_url: str) -> str:
    """Only ever redirect to a same-site path -- an unchecked `next` param
    is a classic open-redirect/phishing vector (send a victim to a trusted
    login page that then bounces them to an attacker-controlled site).

    Checks the backslash-normalized form too, not just the raw string:
    browsers resolving a Location header normalize a leading backslash to
    a forward slash for special schemes (http/https), so "/\\evil.com" --
    which doesn't start with "//" itself -- becomes the protocol-relative
    "//evil.com" by the time the browser actually navigates, the same
    external-redirect bypass a bare "//" check alone would miss."""
    if not next_url or not next_url.startswith("/"):
        return LOGIN_REDIRECT_URL
    if next_url.replace("\\", "/").startswith("//"):
        return LOGIN_REDIRECT_URL
    return next_url


@router.get("/login", name="login")
async def login_form(next: str = "", ctx: dict = Depends(get_template_context)):
    if ctx["user"] is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(ctx["request"], "accounts/login.html", {**ctx, "form": LoginForm(), "next": next})


@router.post("/login")
async def login_submit(
    request: Request, session: Session = Depends(get_session), db: DBSession = Depends(get_db),
    ctx: dict = Depends(get_template_context),
):
    formdata = await request.form()
    form = LoginForm(formdata=formdata)
    next_url = str(formdata.get("next", ""))
    # Deliberately the same message whether the form itself failed to
    # validate (e.g. an empty field) or the credentials were wrong -- never
    # more specific than that (matches Django's AuthenticationForm), so a
    # blank submission doesn't quietly render with no explanation at all.
    error = "Incorrect username or password."

    if form.validate():
        user = db.query(User).filter_by(username=form.username.data).one_or_none()
        if user is not None and user.is_active and security.verify_password(form.password.data, user.password_hash):
            redirect = RedirectResponse(_safe_next(next_url), status_code=303)
            auth.login_user(db, redirect, session, user, form.remember_me.data)
            return redirect

    return templates.TemplateResponse(
        request, "accounts/login.html", {**ctx, "form": form, "next": next_url, "error": error}, status_code=400,
    )


@router.post("/logout")
async def logout(session: Session = Depends(get_session), db: DBSession = Depends(get_db)):
    redirect = RedirectResponse("/accounts/login", status_code=303)
    auth.logout_user(db, redirect, session)
    return redirect


@router.get("/invite", name="invite")
async def invite_form(user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse(ctx["request"], "accounts/invite.html", {**ctx, "form": InviteUserForm()})


@router.post("/invite")
async def invite_submit(
    request: Request, background_tasks: BackgroundTasks,
    user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
    db: DBSession = Depends(get_db), ctx: dict = Depends(get_template_context),
):
    form = InviteUserForm(formdata=await request.form())
    is_valid = form.validate()
    if _reject_duplicate_username(form, db):
        is_valid = False

    new_user = None
    if is_valid:
        new_user = User(
            username=form.username.data, email=form.email.data,
            first_name=form.first_name.data or "", last_name=form.last_name.data or "",
            department=form.department.data or "", is_staff=form.is_staff.data,
            is_active=True, password_hash=security.unusable_password(),
        )
        is_valid = _add_user_or_reject(db, form, new_user)

    if not is_valid:
        return templates.TemplateResponse(request, "accounts/invite.html", {**ctx, "form": form}, status_code=400)

    token = security.make_account_token(new_user.id, new_user.password_hash)
    activate_url = str(request.url_for("activate_account", token=token))
    # BackgroundTasks (not an awaited call): send_mail is sync (smtplib, up
    # to a 10s timeout) and best-effort -- never raises, and the response
    # below doesn't depend on whether it succeeds (the flash message is
    # shown regardless, see below). FastAPI runs it in a threadpool AFTER
    # the response is sent, so a slow/unreachable SMTP relay adds zero
    # latency to this request instead of blocking the event loop in front
    # of it.
    background_tasks.add_task(
        email_backend.send_mail,
        subject="You've been invited to HERMES",
        body=f"An account has been created for you on HERMES.\n\nSet your password to activate it: {activate_url}",
        to=new_user.email,
    )
    # Load-bearing, not cosmetic: with no SMTP configured (the default,
    # see settings.py), this flash message is the ONLY way a data
    # custodian retrieves a new hire's activation link.
    flash(session, "success", f"Invited {new_user.username}. Activation link: {activate_url}")
    return RedirectResponse(request.url_for("invite"), status_code=303)


@router.get("/users/create", name="create_user")
async def create_user_form(user: User = Depends(require_data_custodian), ctx: dict = Depends(get_template_context)):
    return templates.TemplateResponse(ctx["request"], "accounts/create_user.html", {**ctx, "form": CreateUserForm()})


@router.post("/users/create")
async def create_user_submit(
    request: Request, user: User = Depends(require_data_custodian), session: Session = Depends(get_session),
    db: DBSession = Depends(get_db), ctx: dict = Depends(get_template_context),
):
    form = CreateUserForm(formdata=await request.form())
    is_valid = form.validate()
    if _reject_duplicate_username(form, db):
        is_valid = False

    if is_valid:
        new_user = User(
            username=form.username.data, email=form.email.data or "",
            first_name=form.first_name.data or "", last_name=form.last_name.data or "",
            department=form.department.data or "", is_staff=form.is_staff.data,
            is_active=True, password_hash=security.hash_password(form.password1.data),
        )
        is_valid = _add_user_or_reject(db, form, new_user)

    if not is_valid:
        return templates.TemplateResponse(request, "accounts/create_user.html", {**ctx, "form": form}, status_code=400)

    flash(session, "success", f"Created account for {new_user.username}. They can sign in immediately.")
    return RedirectResponse(request.url_for("user_list"), status_code=303)


@router.get("/activate/{token}", name="activate_account")
async def activate_form(token: str, db: DBSession = Depends(get_db), ctx: dict = Depends(get_template_context)):
    user = _resolve_activation_token(db, token)
    if user is None:
        return templates.TemplateResponse(ctx["request"], "accounts/activate_invalid.html", ctx, status_code=400)
    return templates.TemplateResponse(ctx["request"], "accounts/activate.html", {**ctx, "form": ActivateForm()})


@router.post("/activate/{token}")
async def activate_submit(
    token: str, request: Request, session: Session = Depends(get_session), db: DBSession = Depends(get_db),
    ctx: dict = Depends(get_template_context),
):
    user = _resolve_activation_token(db, token)
    if user is None:
        return templates.TemplateResponse(request, "accounts/activate_invalid.html", ctx, status_code=400)

    form = ActivateForm(formdata=await request.form())
    # form.validate() must run exactly once: WTForms rebuilds Field.errors
    # from scratch (discarding anything appended since) on every call, so a
    # second call here would silently wipe the strength errors appended
    # below -- see _reject_duplicate_username's docstring for the same
    # tuple-vs-list half of this WTForms behavior.
    is_valid = form.validate()
    if is_valid:
        strength_errors = security.password_strength_errors(
            form.password1.data, username=user.username, email=user.email,
            first_name=user.first_name, last_name=user.last_name,
        )
        form.password1.errors.extend(strength_errors)
        is_valid = not strength_errors

    if is_valid:
        user.password_hash = security.hash_password(form.password1.data)
        redirect = RedirectResponse(LOGIN_REDIRECT_URL, status_code=303)
        auth.login_user(db, redirect, session, user, remember=True)
        return redirect

    return templates.TemplateResponse(request, "accounts/activate.html", {**ctx, "form": form}, status_code=400)


def _resolve_activation_token(db: DBSession, token: str) -> User | None:
    data = security.read_account_token(token)
    if data is None:
        return None
    user = db.get(User, data["uid"])
    if user is None or not user.is_active or not security.account_token_matches(data, user.password_hash):
        return None
    return user


@router.get("/users", name="user_list")
async def user_list(
    user: User = Depends(require_data_custodian), db: DBSession = Depends(get_db),
    ctx: dict = Depends(get_template_context),
):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse(ctx["request"], "accounts/user_list.html", {**ctx, "users": users})
