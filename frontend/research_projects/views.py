from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import redirect, render

from hermes_frontend import backend_client
from research_projects.forms import AddMemberForm, CreateProjectForm, ProjectDocumentForm, ReviewProjectForm
from research_projects.models import ProjectDocument


def _is_data_custodian(user) -> bool:
    return user.is_active and user.is_staff


@login_required
def project_list(request):
    try:
        projects = backend_client.list_projects(username=request.user.username)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load projects: {e.detail}")
        projects = []
    return render(request, "research_projects/list.html", {"projects": projects})


@login_required
def project_create(request):
    if request.method == "POST":
        form = CreateProjectForm(request.POST)
        if form.is_valid():
            try:
                project = backend_client.create_project(
                    title=form.cleaned_data["title"],
                    created_by=request.user.username,
                    description=form.cleaned_data["description"],
                    ethics_reference=form.cleaned_data["ethics_reference"],
                )
            except backend_client.BackendError as e:
                messages.error(request, f"Could not create project: {e.detail}")
            else:
                messages.success(request, "Project created as a draft. Submit it for review when ready.")
                return redirect("research_projects:detail", project_id=project["project_id"])
    else:
        form = CreateProjectForm()
    return render(request, "research_projects/create.html", {"form": form})


@login_required
def project_detail(request, project_id):
    try:
        project = backend_client.get_project(project_id)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load project: {e.detail}")
        return redirect("research_projects:list")

    is_member = any(m["username"] == request.user.username for m in project["members"])
    documents = ProjectDocument.objects.filter(project_id=project_id)
    try:
        jobs = backend_client.list_project_jobs(project_id)
    except backend_client.BackendError:
        jobs = []

    return render(request, "research_projects/detail.html", {
        "project": project,
        "is_member": is_member,
        "documents": documents,
        "jobs": jobs,
        "document_form": ProjectDocumentForm(),
        "add_member_form": AddMemberForm(),
        "review_form": ReviewProjectForm(),
    })


@login_required
def project_submit(request, project_id):
    if request.method == "POST":
        try:
            backend_client.submit_project(project_id, request.user.username)
        except backend_client.BackendError as e:
            messages.error(request, f"Could not submit project: {e.detail}")
        else:
            messages.success(request, "Project submitted for review.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
@user_passes_test(_is_data_custodian)
def review_queue(request):
    try:
        pending = backend_client.list_projects(status="submitted")
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load review queue: {e.detail}")
        pending = []
    return render(request, "research_projects/review_queue.html", {"projects": pending})


@login_required
@user_passes_test(_is_data_custodian)
def project_review(request, project_id):
    if request.method == "POST":
        form = ReviewProjectForm(request.POST)
        if form.is_valid():
            approved = form.cleaned_data["decision"] == "approve"
            try:
                backend_client.review_project(
                    project_id, reviewer=request.user.username, approved=approved,
                    comment=form.cleaned_data["comment"], expiry_date=form.cleaned_data["expiry_date"],
                )
            except backend_client.BackendError as e:
                messages.error(request, f"Could not review project: {e.detail}")
            else:
                messages.success(request, f"Project {'approved' if approved else 'rejected'}.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
@user_passes_test(_is_data_custodian)
def project_revoke(request, project_id):
    if request.method == "POST":
        comment = request.POST.get("comment", "")
        try:
            backend_client.revoke_project(project_id, revoked_by=request.user.username, comment=comment)
        except backend_client.BackendError as e:
            messages.error(request, f"Could not revoke project: {e.detail}")
        else:
            messages.success(request, "Project revoked.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
def project_add_member(request, project_id):
    if request.method == "POST":
        form = AddMemberForm(request.POST)
        if form.is_valid():
            try:
                backend_client.add_member(
                    project_id, form.cleaned_data["username"],
                    added_by=request.user.username, role=form.cleaned_data["role"],
                )
            except backend_client.BackendError as e:
                messages.error(request, f"Could not add member: {e.detail}")
            else:
                messages.success(request, f"Added {form.cleaned_data['username']} to the project.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
def project_remove_member(request, project_id, username):
    if request.method == "POST":
        try:
            backend_client.remove_member(project_id, username, removed_by=request.user.username)
        except backend_client.BackendError as e:
            messages.error(request, f"Could not remove member: {e.detail}")
        else:
            messages.success(request, f"Removed {username} from the project.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
def upload_document(request, project_id):
    if request.method == "POST":
        form = ProjectDocumentForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.project_id = project_id
            doc.uploaded_by = request.user.username
            doc.save()
            messages.success(request, "Document uploaded.")
    return redirect("research_projects:detail", project_id=project_id)


@login_required
@user_passes_test(_is_data_custodian)
def all_projects(request):
    """Every project regardless of status or membership -- staff only.
    Users can't hide a project from an admin: there's no membership filter
    here at all, unlike project_list (which is always scoped to "mine")."""
    status = request.GET.get("status") or None
    try:
        projects = backend_client.list_projects(status=status)
    except backend_client.BackendError as e:
        messages.error(request, f"Could not load projects: {e.detail}")
        projects = []
    return render(request, "research_projects/all_projects.html", {"projects": projects, "status": status})
