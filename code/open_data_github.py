# -*- coding: utf-8 -*-
"""把导出目录以「整树快照」的方式发布到 GitHub（走 REST Git Data API）。

为什么不用 git push：本机的 schannel / curl 对 github.com 的 TLS 握手失败
（SEC_E_NO_CREDENTIALS），git/curl 都出不去；只有 Python(OpenSSL) 能到
api.github.com。所以这里用 Git Data API 直接建 blob / tree / commit / ref。

特性：
  * 只上传内容变化的文件（按 git blob sha1 比对远端 tree）；
  * 整棵树作为一次 commit，远端被删掉的文件自动消失；
  * 大文件逐个上传（GitHub 单 blob 上限 100MB）。

用法：
  python ai_quant/open_data_github.py --dry-run
  python ai_quant/open_data_github.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, r"D:\AI_Quant_System\ai_quant")
from gh_auth import token  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_DIR = Path(os.environ.get("OPEN_DATA_OUT") or r"D:\AI_Quant_System\public\ashare-open-data")
REPO_NAME = "ashare-open-data"
DESCRIPTION = "A 股日线/分钟行情 + 模拟盘每日复盘账本 + 影子盘前瞻样本（每交易日自动更新）"
BRANCH = "main"
SKIP_DIRS = {".git"}


def _req(path: str, method: str = "GET", body: dict | None = None,
         timeout: int = 300, retries: int = 3) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    last = (0, {})
    for attempt in range(retries):
        req = urllib.request.Request("https://api.github.com" + path, data=data, method=method)
        req.add_header("Authorization", "token " + token())
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "AI-Quant-System/open-data")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                payload = {"message": raw[:300]}
            if e.code < 500:
                return e.code, payload
            last = (e.code, payload)
        except Exception as e:  # 网络抖动 / 超时
            last = (0, {"error": f"{type(e).__name__}: {e}"})
        time.sleep(1.5 * (attempt + 1))
    return last


def blob_sha(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest(), len(data)


def local_files() -> dict[str, tuple[Path, str, int]]:
    out: dict[str, tuple[Path, str, int]] = {}
    for p in sorted(REPO_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO_DIR)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        sha, size = blob_sha(p)
        out[rel.as_posix()] = (p, sha, size)
    return out


def ensure_repo(dry: bool) -> dict:
    st, me = _req("/user")
    if st != 200:
        raise SystemExit(f"GET /user -> {st} {me}")
    login = me["login"]
    st, info = _req(f"/repos/{login}/{REPO_NAME}")
    if st == 200:
        return {"login": login, "created": False, "url": info["html_url"],
                "push": info["permissions"].get("push", False)}
    if st != 404:
        raise SystemExit(f"GET repo -> {st} {info}")
    if dry:
        return {"login": login, "created": True, "url": f"https://github.com/{login}/{REPO_NAME}",
                "push": None, "dry": True}
    st, info = _req("/user/repos", "POST", {
        "name": REPO_NAME, "description": DESCRIPTION, "private": False,
        "has_issues": True, "has_wiki": False, "auto_init": False,
    })
    if st != 201:
        raise SystemExit(f"POST repo -> {st} {info}")
    return {"login": login, "created": True, "url": info["html_url"], "push": True}


def remote_state(login: str) -> tuple[str | None, dict[str, str]]:
    """返回 (head_sha, path->blob_sha)。空仓库返回 (None, {})。"""
    st, ref = _req(f"/repos/{login}/{REPO_NAME}/git/ref/heads/{BRANCH}")
    if st in (404, 409):
        return None, {}
    if st != 200:
        raise SystemExit(f"GET ref -> {st} {ref}")
    head = ref["object"]["sha"]
    st, tree = _req(f"/repos/{login}/{REPO_NAME}/git/trees/{head}?recursive=1")
    if st != 200:
        raise SystemExit(f"GET tree -> {st} {tree}")
    return head, {e["path"]: e["sha"] for e in tree.get("tree", []) if e["type"] == "blob"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--message", default="")
    args = ap.parse_args()

    if not REPO_DIR.exists():
        raise SystemExit(f"导出目录不存在：{REPO_DIR}")
    if not token():
        raise SystemExit("没有 GitHub 令牌")

    repo = ensure_repo(args.dry_run)
    login = repo["login"]
    head, remote = remote_state(login) if not repo["created"] else (None, {})

    if head is None and not args.dry_run:
        # 空仓库不允许直接建 blob/tree（409 Git Repository is empty）：先落一个初始提交
        st, _ = _req(f"/repos/{login}/{REPO_NAME}/contents/.init", "PUT", {
            "message": "init: empty repository", "content": base64.b64encode(b"init\n").decode(),
        })
        if st not in (200, 201):
            raise SystemExit(f"seed commit -> {st}")
        head, remote = remote_state(login)

    files = local_files()
    if not files:
        raise SystemExit("没有可发布的文件")

    changed = {k: v for k, v in files.items() if remote.get(k) != v[1]}
    deleted = sorted(set(remote) - set(files))
    total_mb = sum(v[2] for v in files.values()) / 1e6

    plan = {
        "repo": repo["url"], "created": repo["created"],
        "files": len(files), "total_mb": round(total_mb, 1),
        "changed": len(changed), "changed_mb": round(sum(v[2] for v in changed.values()) / 1e6, 1),
        "deleted": len(deleted),
    }
    if args.dry_run:
        plan["dry_run"] = True
        plan["changed_sample"] = sorted(changed)[:12]
        plan["deleted_sample"] = deleted[:12]
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    # 非交易日：只有 manifest 的时间戳变了，不制造空提交
    if changed and set(changed) == {"manifest.json"} and not deleted:
        plan.update({"ok": True, "noop": True})
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    # 1) 上传变化的 blob（并发）
    shas: dict[str, str] = {k: v[1] for k, v in files.items()}
    errors: list[str] = []

    def upload(item):
        rel, (path, sha, _size) = item
        b64 = base64.b64encode(path.read_bytes()).decode()
        last = None
        for attempt in range(3):
            st, r = _req(f"/repos/{login}/{REPO_NAME}/git/blobs", "POST",
                         {"content": b64, "encoding": "base64"})
            if st == 201:
                return rel, None
            last = f"{st} {r.get('message', r)}"
            if st in (401, 409, 429) or st >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        return rel, last

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(upload, kv) for kv in changed.items()]
        for i, f in enumerate(as_completed(futs), 1):
            rel, err = f.result()
            if err:
                errors.append(f"{rel}: {err}")
            if i % 25 == 0:
                print(f"  uploaded {i}/{len(changed)}", flush=True)
    if errors:
        print(json.dumps({"ok": False, "errors": errors[:10]}, ensure_ascii=False, indent=2))
        return 1

    # 2) 整树快照（不再挂 base_tree：远端多余文件自动删除）
    entries = [{"path": k, "mode": "100644", "type": "blob", "sha": shas[k]} for k in sorted(shas)]
    st, tree = _req(f"/repos/{login}/{REPO_NAME}/git/trees", "POST", {"tree": entries}, timeout=300)
    if st != 201:
        print(json.dumps({"ok": False, "step": "tree", "status": st, "resp": tree},
                         ensure_ascii=False, indent=2))
        return 1
    tree_sha = tree["sha"]

    # 3) commit + ref
    msg = args.message or f"data: daily refresh {time.strftime('%Y-%m-%d')}"
    payload = {"message": msg, "tree": tree_sha, "parents": [head] if head else []}
    st, commit = _req(f"/repos/{login}/{REPO_NAME}/git/commits", "POST", payload)
    if st != 201:
        print(json.dumps({"ok": False, "step": "commit", "status": st, "resp": commit},
                         ensure_ascii=False, indent=2))
        return 1
    commit_sha = commit["sha"]

    if head:
        st, ref = _req(f"/repos/{login}/{REPO_NAME}/git/refs/heads/{BRANCH}", "PATCH",
                       {"sha": commit_sha, "force": False})
    else:
        st, ref = _req(f"/repos/{login}/{REPO_NAME}/git/refs", "POST",
                       {"ref": f"refs/heads/{BRANCH}", "sha": commit_sha})
    if st not in (200, 201):
        print(json.dumps({"ok": False, "step": "ref", "status": st, "resp": ref},
                         ensure_ascii=False, indent=2))
        return 1

    plan.update({"ok": True, "commit": commit_sha, "tree": tree_sha,
                 "commit_url": f"{repo['url']}/commit/{commit_sha}"})
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
