#!/usr/bin/env python3
"""Public GitHub image intake. No credentials, uploads, cloning or remote execution."""
import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
import warnings

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://github.com/9federal/meme-sites"
MAX_BYTES = 10 * 1024 * 1024
EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def now():
    return datetime.now(timezone.utc).isoformat()


def local(path):
    target = ROOT / path
    if not target.resolve().is_relative_to(ROOT):
        raise ValueError("Local path escapes the skill")
    if any(p.is_symlink() for p in [target, *target.parents] if p != ROOT.parent):
        raise ValueError("Symlinks are not writable intake targets")
    return target


def read(path):
    return json.loads(local(path).read_text())


def save(path, value):
    target = local(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
        temp = f.name
    os.replace(temp, target)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("GitHub redirect requires a fresh source review")


def api(path):
    # The only outbound destination; never send local contents or credentials.
    request = Request("https://api.github.com/" + path, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "meme-sites-public-image-intake",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        data = response.read(24 * 1024 * 1024 + 1)
    if len(data) > 24 * 1024 * 1024:
        raise ValueError("API response too large; narrow the review")
    return json.loads(data)


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Expected GitHub owner/repository")
    return value


def public_repo(repo):
    data = api("repos/" + repo_name(repo))
    if data.get("private") is not False or data.get("visibility") != "public":
        raise ValueError("Only verified public repositories are eligible")
    if data["full_name"].lower() != repo.lower():
        raise ValueError("Repository identity changed; review again")
    return data


def commit(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("An immutable 40-character commit is required")
    return value


def remote_path(value):
    path = PurePosixPath(value)
    if not value or value.startswith("/") or ".." in path.parts or "\\" in value:
        raise ValueError("Expected repository-relative path")
    return str(path)


def file_at(repo, revision, path):
    data = api(f"repos/{repo_name(repo)}/contents/{quote(remote_path(path), safe='/')}?ref={commit(revision)}")
    if data.get("type") != "file" or data.get("encoding") != "base64":
        raise ValueError("Only ordinary inline GitHub files are accepted")
    if data.get("size", MAX_BYTES + 1) > MAX_BYTES:
        raise ValueError("File exceeds 10 MiB")
    payload = base64.b64decode(data["content"], validate=False)
    if len(payload) > MAX_BYTES:
        raise ValueError("File exceeds 10 MiB")
    return payload, data["sha"]


def source_url(repo, revision, path):
    return f"https://github.com/{repo}/blob/{revision}/{quote(path, safe='/')}"


def verify_usage(project, revision):
    proof, _ = file_at(project["repo"], revision, project["evidence_path"])
    text = proof.decode("utf-8")
    # A link alone isn't proof: the maintainer must also review the actual project.
    if not re.search(re.escape(ORIGIN) + r"(?:\.git)?(?=$|[\s\)\]<>\"'`/#])", text):
        raise ValueError("Exact upstream attribution missing")
    if not project.get("usage_note", "").strip() or not project.get("image_roots"):
        raise ValueError("Missing reviewed project usage or image scope")


def checkpoint(section, value):
    path = "community/last-check.json"
    previous = read(path) if local(path).exists() else {}
    previous[section] = {"checked_at": now(), **value}
    save(path, previous)


def discover(_args):
    origin_repo = ORIGIN.removeprefix("https://github.com/")
    public_repo(origin_repo)
    forks = []
    for page in range(1, 11):
        batch = api(f"repos/{origin_repo}/forks?per_page=100&page={page}")
        forks.extend(x["html_url"] for x in batch if x.get("private") is False)
        if len(batch) < 100:
            break
    else:
        raise ValueError("Fork pagination incomplete; continue with MCP/browser")
    result = {"scope": "public forks only; combine with exact-upstream code search",
              "unverified_repositories": forks}
    checkpoint("fork_discovery", result)
    return result


def add_project(args):
    sources = read("community/sources.json")
    repo = repo_name(args.repo)
    if repo.lower() == ORIGIN.removeprefix("https://github.com/").lower():
        raise ValueError("The skill's own repository is not a user project")
    public_repo(repo)
    project = {"repo": repo, "evidence_commit": commit(args.commit),
               "evidence_path": remote_path(args.evidence_path),
               "usage_note": args.usage_note, "verified_at": now(),
               "image_roots": [remote_path(x).rstrip("/") for x in args.image_root]}
    if any(root == "." for root in project["image_roots"]):
        raise ValueError("Scope a specific reviewed image directory")
    verify_usage(project, args.commit)
    if any(p["repo"].lower() == repo.lower() for p in sources["projects"]):
        raise ValueError("Project already registered; review and edit its existing entry")
    sources["projects"].append(project)
    save("community/sources.json", sources)
    return {"registered": repo}


def scan(_args):
    candidates = read("community/candidates.json")
    seen = {c["id"] for c in candidates}
    added, checked, blockers = 0, 0, []
    for project in read("community/sources.json")["projects"]:
        repo = project["repo"]
        try:
            metadata = public_repo(repo)
            head = api(f"repos/{repo}/commits/{quote(metadata['default_branch'], safe='')}")["sha"]
            verify_usage(project, head)
            tree = api(f"repos/{repo}/git/trees/{head}?recursive=1")
            if tree.get("truncated"):
                raise ValueError("Tree truncated; review a narrower source manually")
            for item in tree["tree"]:
                path = item["path"]
                if not any(path.startswith(root + "/") for root in project["image_roots"]):
                    continue
                if item["type"] != "blob" or item.get("mode") != "100644":
                    continue  # Includes symlinks, submodules and executable files.
                if PurePosixPath(path).suffix.lower() not in EXTENSIONS:
                    continue
                if not 0 < item.get("size", MAX_BYTES + 1) <= MAX_BYTES:
                    continue
                key = hashlib.sha256(f"{repo.lower()}\n{path}\n{item['sha']}".encode()).hexdigest()
                if key not in seen:
                    candidates.append({"id": key, "repo": repo, "commit": head,
                                       "path": path, "blob": item["sha"], "size": item["size"],
                                       "url": source_url(repo, head, path), "status": "pending",
                                       "rights": None, "found_at": now()})
                    seen.add(key)
                    added += 1
            checked += 1
        except Exception as error:
            blockers.append({"repo": repo, "reason": str(error)})
    save("community/candidates.json", candidates)
    result = {"projects_checked": checked, "new_links": added, "blockers": blockers}
    checkpoint("scan", result)
    return result


def raster(payload):
    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise ValueError("Import requires maintained Pillow; discovery/scan need only Python") from error
    Image.MAX_IMAGE_PIXELS = 16_000_000
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(payload)) as image:
            if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                raise ValueError("Only decoded PNG/JPEG/GIF/WebP are accepted")
            if image.width * image.height > 16_000_000 or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Animated or oversized image: leave a link for manual review")
            image.load()
            clean = ImageOps.exif_transpose(image).convert("RGBA")
            pixels = hashlib.sha256(f"{clean.width}x{clean.height}:".encode() + clean.tobytes()).hexdigest()
            # Re-encode pixels, stripping active trailers, metadata and embedded instructions.
            output = io.BytesIO()
            clean.save(output, format="PNG")
            return output.getvalue(), pixels


def import_image(args):
    candidates = read("community/candidates.json")
    candidate = next(c for c in candidates if c["id"] == args.id)
    project = next(p for p in read("community/sources.json")["projects"]
                   if p["repo"].lower() == candidate["repo"].lower())
    public_repo(candidate["repo"])
    verify_usage(project, candidate["commit"])
    if PurePosixPath(candidate["path"]).suffix.lower() not in EXTENSIONS:
        raise ValueError("Unsupported image extension")
    if not any(candidate["path"].startswith(root + "/") for root in project["image_roots"]):
        raise ValueError("Image outside reviewed project scope")
    # Rights must be reviewed locally. Foreign manifests cannot approve themselves.
    evidence, _ = file_at(candidate["repo"], args.rights_commit, args.rights_path)
    quote_text = args.rights_quote.strip()
    if len(quote_text) < 20 or quote_text not in evidence.decode("utf-8"):
        raise ValueError("Missing exact public image-rights evidence")
    if not args.rights_note.strip() or not args.credit.strip():
        raise ValueError("Record rights-holder/scope reasoning and required attribution")
    payload, blob = file_at(candidate["repo"], candidate["commit"], candidate["path"])
    if blob != candidate["blob"]:
        raise ValueError("Image blob changed")
    expected = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
    if expected != blob:
        raise ValueError("Image content does not match pinned Git blob")
    clean, pixels = raster(payload)
    sha = hashlib.sha256(payload).hexdigest()
    catalog = read("assets/memes/catalog.json")
    # Detect existing unindexed/manual rasters without changing them.
    duplicate = None  # Inspect current pixels: catalog hashes may predate a manual edit.
    for existing in local("assets").rglob("*"):
        if duplicate:
            break
        if existing.is_file() and existing.suffix.lower() in EXTENSIONS:
            if existing.is_symlink() or not existing.resolve().is_relative_to(ROOT):
                continue
            if existing.stat().st_size > MAX_BYTES:
                continue
            original = existing.read_bytes()
            if hashlib.sha256(original).hexdigest() == sha:
                duplicate = str(existing.relative_to(ROOT))
                break
            try:
                if raster(original)[1] == pixels:
                    duplicate = str(existing.relative_to(ROOT))
            except Exception:
                continue
    rights = {"license": args.license, "credit": args.credit, "note": args.rights_note,
              "evidence_url": source_url(candidate["repo"], args.rights_commit, args.rights_path),
              "evidence_quote": quote_text, "reviewed_at": now()}
    output = duplicate or f"assets/memes/{pixels}.png"
    if not duplicate:
        # Exclusive creation prevents overwriting a manually edited or interrupted output.
        target = local(output)
        with target.open("xb") as f:
            f.write(clean)
    # Recover a previously written output after interruption without overwriting it.
    if (not duplicate or duplicate == f"assets/memes/{pixels}.png") and not any(
            x["file"] == output for x in catalog):
        catalog.append({"file": output, "source_sha256": sha, "pixels_sha256": pixels,
                        "source_url": candidate["url"], "source_repo": candidate["repo"],
                        "source_commit": candidate["commit"], "source_blob": blob,
                        "rights": rights, "imported_at": now()})
        save("assets/memes/catalog.json", catalog)
    candidate.update(status="duplicate" if duplicate else "imported", file=output, rights=rights)
    save("community/candidates.json", candidates)
    return {"status": candidate["status"], "file": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover").set_defaults(run=discover)
    commands.add_parser("scan").set_defaults(run=scan)
    add = commands.add_parser("add-project")
    for field in ["repo", "commit", "evidence-path", "usage-note"]:
        add.add_argument("--" + field, required=True)
    add.add_argument("--image-root", action="append", required=True)
    add.set_defaults(run=add_project)
    imp = commands.add_parser("import")
    for field in ["id", "rights-commit", "rights-path", "rights-quote", "rights-note", "credit"]:
        imp.add_argument("--" + field, required=True)
    imp.add_argument("--license", required=True,
                     choices=["CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "MIT", "Apache-2.0", "permission"])
    imp.add_argument("--confirm-image-rights", required=True, action="store_true",
                     help="Maintainer reviewed actual image rights, not merely the code license")
    imp.set_defaults(run=import_image)
    args = parser.parse_args()
    with local("community/.intake.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read("community/sources.json")["origin"] != ORIGIN:
            raise ValueError("Unexpected upstream identity")
        result = args.run(args)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result.get("blockers") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"blocker": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
