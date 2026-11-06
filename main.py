import os
import markdown
from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import auth
import database
import git_utils
import models

REPOS_DIR = "./repos"
os.makedirs(REPOS_DIR, exist_ok=True)
models.Base.metadata.create_all(bind=database.engine)  # creates tables on first run

app = FastAPI(title="GitClone")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
get_db = database.get_db


# ---------------- helpers ----------------
def render(request, name, user, **ctx):
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def error(request, user, message, status=404):
    return templates.TemplateResponse(
        request, "error.html", {"user": user, "message": message}, status_code=status
    )


def load_repo(db, username, repo_name, user):
    """Return (repo, path) or (None, None). Private repos are hidden from other people."""
    owner = db.query(models.User).filter(models.User.username == username).first()
    if not owner or not git_utils.valid_name(repo_name):
        return None, None
    repo = db.query(models.Repository).filter_by(owner_id=owner.id, name=repo_name).first()
    if not repo:
        return None, None
    if repo.is_private and (not user or user.id != owner.id):
        return None, None
    return repo, git_utils.repo_path(REPOS_DIR, username, repo_name)


def pick_branch(path, branch):
    """Use the requested branch if it exists, otherwise main, otherwise the first one."""
    branches = git_utils.get_branches(path)
    if branch in branches:
        return branch, branches
    if "main" in branches:
        return "main", branches
    return (branches[0] if branches else "main"), branches


# ---------------- home / auth ----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "index.html", None)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "register.html", None)


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not git_utils.valid_name(username):
        return render(
            request, "register.html", None, error="Username: letters, numbers, . _ - only"
        )
    exists = (
        db.query(models.User)
        .filter((models.User.username == username) | (models.User.email == email))
        .first()
    )
    if exists:
        return render(request, "register.html", None, error="Username or email already taken")
    db.add(
        models.User(
            username=username, email=email, hashed_password=auth.get_password_hash(password)
        )
    )
    db.commit()
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not auth.verify_password(password, user.hashed_password):
        return render(request, "login.html", None, error="Invalid username or password")
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        "access_token", auth.create_access_token(user.username), httponly=True, samesite="lax"
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("access_token")
    return response


# ---------------- dashboard / search / create repo ----------------
# NOTE: fixed paths like /dashboard and /repo/new must come BEFORE /{username}
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    repos = db.query(models.Repository).filter_by(owner_id=user.id).all()
    return render(request, "dashboard.html", user, repos=repos)

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    results = []
    if q.strip():
        results = (
            db.query(models.Repository)
            .join(models.User)
            .filter(models.Repository.is_private == False)  # noqa: E712
            .filter(models.Repository.name.ilike(f"%{q}%"))
            .all()
        )
    return render(request, "search_results.html", user, q=q, results=results)


@app.get("/repo/new", response_class=HTMLResponse)
def new_repo_page(request: Request, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "new_repo.html", user)


@app.post("/repo/new")
def new_repo(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    is_private: str = Form(None),
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not git_utils.valid_name(name):
        return render(request, "new_repo.html", user, error="Name: letters, numbers, . _ - only")
    if db.query(models.Repository).filter_by(owner_id=user.id, name=name).first():
        return render(
            request, "new_repo.html", user, error="You already have a repo with that name"
        )
    db.add(
        models.Repository(
            name=name, description=description, is_private=bool(is_private), owner_id=user.id
        )
    )
    db.commit()
    git_utils.create_bare_repo(REPOS_DIR, user.username, name)
    return RedirectResponse(f"/{user.username}/{name}", status_code=303)


# ---------------- user profile ----------------
@app.get("/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    owner = db.query(models.User).filter(models.User.username == username).first()
    if not owner:
        return error(request, user, "User not found")
    repos = db.query(models.Repository).filter_by(owner_id=owner.id).all()
    if not user or user.id != owner.id:
        repos = [r for r in repos if not r.is_private]
    return render(request, "user_profile.html", user, owner=owner, repos=repos)


# ---------------- code browser ----------------
@app.get("/{username}/{repo_name}", response_class=HTMLResponse)
def repo_home(
    request: Request,
    username: str,
    repo_name: str,
    branch: str = "main",
    db: Session = Depends(get_db),
):
    return browse(request, username, repo_name, "", branch, db)


@app.get("/{username}/{repo_name}/tree/{subpath:path}", response_class=HTMLResponse)
def repo_tree(
    request: Request,
    username: str,
    repo_name: str,
    subpath: str,
    branch: str = "main",
    db: Session = Depends(get_db),
):
    return browse(request, username, repo_name, subpath, branch, db)


def browse(request, username, repo_name, subpath, branch, db):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo:
        return error(request, user, "Repository not found")
    empty = git_utils.is_repo_empty(path)
    branch, branches = pick_branch(path, branch)
    items = [] if empty else git_utils.get_tree(path, branch, subpath)
    readme_html = None
    if not subpath:
        for item in items:
            if item["name"].lower() == "readme.md":
                text = git_utils.get_file_content(path, branch, item["path"])
                if text:
                    readme_html = markdown.markdown(text, extensions=["fenced_code"])
    starred = bool(
        user and db.query(models.Star).filter_by(user_id=user.id, repo_id=repo.id).first()
    )
    parts = subpath.split("/") if subpath else []
    crumbs = [{"name": p, "path": "/".join(parts[: i + 1])} for i, p in enumerate(parts)]
    return render(
        request,
        "repo_detail.html",
        user,
        repo=repo,
        username=username,
        items=items,
        empty=empty,
        branch=branch,
        branches=branches,
        readme_html=readme_html,
        stars=len(repo.stars),
        starred=starred,
        subpath=subpath,
        crumbs=crumbs,
        is_owner=bool(user and user.username == username),
        clone_url=f"{request.base_url}{username}/{repo_name}",
    )


@app.get("/{username}/{repo_name}/blob/{filepath:path}", response_class=HTMLResponse)
def view_file(
    request: Request,
    username: str,
    repo_name: str,
    filepath: str,
    branch: str = "main",
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo:
        return error(request, user, "Repository not found")
    branch, _ = pick_branch(path, branch)
    content = git_utils.get_file_content(path, branch, filepath)
    if content is None:
        return error(request, user, "File not found (or it is a binary file)")
    return render(
        request,
        "file_view.html",
        user,
        repo=repo,
        username=username,
        filepath=filepath,
        content=content,
        branch=branch,
        lines=content.count("\n") + 1,
    )


@app.get("/{username}/{repo_name}/add-file", response_class=HTMLResponse)
def add_file_page(
    request: Request,
    username: str,
    repo_name: str,
    branch: str = "main",
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo or not user or user.username != username:
        return error(request, user, "Only the owner can add files", 403)
    branch, _ = pick_branch(path, branch)
    return render(request, "add_file.html", user, repo=repo, username=username, branch=branch)


@app.post("/{username}/{repo_name}/add-file")
def add_file(
    request: Request,
    username: str,
    repo_name: str,
    filename: str = Form(...),
    content: str = Form(""),
    message: str = Form("Add file"),
    branch: str = Form("main"),
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo or not user or user.username != username:
        return error(request, user, "Only the owner can add files", 403)
    filename = filename.strip().lstrip("/")
    ok = (
        bool(filename)
        and git_utils.valid_name(branch)
        and git_utils.add_file(
            path, branch, filename, content, message or "Add file", user.username, user.email
        )
    )
    if not ok:
        return render(
            request,
            "add_file.html",
            user,
            repo=repo,
            username=username,
            branch=branch,
            error="Could not save the file. Check the file name.",
        )
    return RedirectResponse(f"/{username}/{repo_name}?branch={branch}", status_code=303)

@app.get("/{username}/{repo_name}/commits", response_class=HTMLResponse)
def commits(
    request: Request,
    username: str,
    repo_name: str,
    branch: str = "main",
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo:
        return error(request, user, "Repository not found")
    branch, branches = pick_branch(path, branch)
    return render(
        request,
        "commits.html",
        user,
        repo=repo,
        username=username,
        branch=branch,
        branches=branches,
        commits=git_utils.get_commits(path, branch),
    )


@app.get("/{username}/{repo_name}/commit/{sha}", response_class=HTMLResponse)
def commit_detail(
    request: Request, username: str, repo_name: str, sha: str, db: Session = Depends(get_db)
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo:
        return error(request, user, "Repository not found")
    details = git_utils.get_commit_details(path, sha) if sha.isalnum() else None
    if not details:
        return error(request, user, "Commit not found")
    return render(request, "commit_detail.html", user, repo=repo, username=username, c=details)


@app.get("/{username}/{repo_name}/branches", response_class=HTMLResponse)
def branches_page(request: Request, username: str, repo_name: str, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo:
        return error(request, user, "Repository not found")
    return render(
        request,
        "branches.html",
        user,
        repo=repo,
        username=username,
        branches=git_utils.get_branches(path),
        is_owner=bool(user and user.username == username),
    )


@app.post("/{username}/{repo_name}/branches/new")
def new_branch(
    request: Request,
    username: str,
    repo_name: str,
    name: str = Form(...),
    from_branch: str = Form("main"),
    db: Session = Depends(get_db),
):
    user = auth.get_current_user(request, db)
    repo, path = load_repo(db, username, repo_name, user)
    if not repo or not user or user.username != username:
        return error(request, user, "Only the owner can create branches", 403)
    if git_utils.valid_name(name):
        git_utils.create_branch(path, name, from_branch)
    return RedirectResponse(f"/{username}/{repo_name}/branches", status_code=303)


# ---------------- stars ----------------
@app.post("/{username}/{repo_name}/star")
def toggle_star(request: Request, username: str, repo_name: str, db: Session = Depends(get_db)):
    user = auth.get_current_user(request, db)
    repo, _ = load_repo(db, username, repo_name, user)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if repo:
        star = db.query(models.Star).filter_by(user_id=user.id, repo_id=repo.id).first()
        if star:
            db.delete(star)
        else:
            db.add(models.Star(user_id=user.id, repo_id=repo.id))
        db.commit()
    return RedirectResponse(f"/{username}/{repo_name}", status_code=303)