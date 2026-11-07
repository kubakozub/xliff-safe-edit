#!/usr/bin/env python3
"""The XLIFF editor's server. Python standard library only, no dependencies.

It binds 127.0.0.1, so it is a single user tool on the machine it runs on. To
reach one running on another machine, forward the port rather than exposing it:

    ssh -L 8070:127.0.0.1:8070 user@your-server

On top of that, HTTP Basic auth. The password is generated on first run into
`data/secret` with mode 600, and the user name is `translator`. State changing
requests also need the X-XLIFF-Edit header, which a browser will not add from
another origin without a preflight, plus an Origin check when one is present.

This is not a multi user server and does not try to be. One person, one machine,
one delivery at a time.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import (ALLOWED_STATES, Package, XliffDoc, parse_glossary, plain,
                    qa_check, real_tags, serialize, tag_inventory, tokenize)

ROOT = os.path.dirname(os.path.abspath(__file__))

#: Where uploads, saved edits and the generated password live. Overridable so a
#: test can point it at a temporary directory instead of the installed package.
DATA = os.environ.get("XE_DATA") or os.path.join(ROOT, "data")
MAX_UPLOAD = 256 * 1024 * 1024
QA_CAP = 20000
LOCK = threading.RLock()
OPEN_DOCS = {}


# --------------------------------------------------------------------------
# magazyn
# --------------------------------------------------------------------------

def _safe_name(name):
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "")
    name = re.sub(r"[\r\n\t]", "_", name).strip()
    name = name.lstrip(".") or "file"
    return name[:200]


def _fid_ok(fid):
    return bool(re.fullmatch(r"[0-9a-f]{16}", fid or ""))


def _fdir(fid):
    if not _fid_ok(fid):
        raise KeyError("bad file id")
    return os.path.join(DATA, fid)


def _read_state(d):
    """Read state.json. A corrupt one is moved aside rather than taking the editor down."""
    p = os.path.join(d, "state.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        try:
            os.replace(p, p + ".broken")
        except OSError:
            pass
        return {}


def load_pkg(fid, fresh=False):
    """Load the package from the original bytes and replay the saved edits onto it."""
    with LOCK:
        if not fresh and fid in OPEN_DOCS:
            return OPEN_DOCS[fid]
        d = _fdir(fid)
        if not os.path.exists(os.path.join(d, "meta.json")):
            raise KeyError("no such file")
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(d, "original.bin"), "rb") as f:
            blob = f.read()
        pkg = Package(blob, meta["name"])
        for key, rec in _read_state(d).get("edits", {}).items():
            try:
                u = pkg.doc.unit(key)
            except KeyError:
                continue
            pool = {t["sref"]: t for t in u["tgt_tokens"] if t["kind"] == "tag"}
            pool.update({t["sref"]: t for t in u["src_tokens"] if t["kind"] == "tag"})
            toks = []
            for t in rec.get("tokens", []):
                if t.get("kind") == "tag":
                    ref = pool.get(t.get("ref"))
                    if ref is None:
                        # An unresolvable tag reference is a hard error. Dropping it quietly would
        # silently change the delivery, which is the one thing this must not do.
                        raise ValueError("segment %s: unknown tag %r in the saved state"
                                         % (key, t.get("ref")))
                    toks.append(ref)
                else:
                    toks.append({"kind": "text", "text": t.get("text", "")})
            pkg.doc.set_target_tokens(key, toks)
            if rec.get("state"):
                try:
                    pkg.doc.set_state(key, rec["state"])
                except ValueError:
                    pass
        OPEN_DOCS[fid] = pkg
        return pkg


def _tok_ref(u, t):
    """A tag's stable identity, assigned at parse time and immune to renumbering."""
    return t.get("sref", t.get("tid"))


def save_state(fid, pkg):
    """An atomic write under the lock. Without it two concurrent saves destroy
    state.json, which was a real defect here and has a regression test."""
    with LOCK:
        d = _fdir(fid)
        edits = {}
        for u in pkg.doc.units:
            if not u["dirty"]:
                continue
            edits[u["key"]] = {
                "tokens": [({"kind": "tag", "ref": _tok_ref(u, t)} if t["kind"] == "tag"
                            else {"kind": "text", "text": t.get("text", "")})
                           for t in u["tgt_tokens"]],
                "state": u["tgt_state"],
            }
        tmp = os.path.join(d, "state.json.%d.%s.tmp" % (os.getpid(), secrets.token_hex(6)))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"edits": edits, "saved": int(time.time())}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(d, "state.json"))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def _tag_label(t):
    if t["kind"] != "tag":
        return ""
    if t.get("role") == "meta":
        return t.get("name", "#")
    inner = re.sub(r"^<[^>]*>|</[^>]*>$", "", t.get("raw", ""))
    inner = re.sub(r"<[^>]*>", "", inner)
    inner = (inner.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")).strip()
    m = re.match(r"</?\s*([\w:.-]+)", inner)
    base = m.group(1) if m else t.get("name", "tag")
    closing = inner.startswith("</") or t.get("role") == "close" or t.get("name") == "ept"
    return ("/" if closing else "") + base + (t.get("id") or "")


def unit_payload(u):
    def tk(toks, src):
        return [{"kind": t["kind"], "text": t.get("text", ""), "name": t.get("name", ""),
                 "id": t.get("id", ""), "role": t.get("role", ""),
                 "ref": t.get("sref") if t["kind"] == "tag" else None,
                 "label": _tag_label(t)} for t in toks]
    return {
        "key": u["key"], "id": u["id"], "translate": u["translate"], "state": u["tgt_state"],
        "qualifier": u["tgt_qualifier"], "dirty": u["dirty"],
        "source": tk(u["src_tokens"], True), "target": tk(u["tgt_tokens"], False),
        "source_text": plain(u["src_tokens"]), "target_text": plain(u["tgt_tokens"]),
    }


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------

def get_password():
    p = os.path.join(DATA, "secret")
    if not os.path.exists(p):
        os.makedirs(DATA, mode=0o700, exist_ok=True)
        pw = secrets.token_urlsafe(18)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pw)
    os.chmod(p, 0o600)
    with open(p) as f:
        return f.read().strip()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "XliffEditor/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # ---- pomocnicze ----

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _drain(self):
        """Drain the body, so a keep-alive connection does not desynchronise on the
        next request."""
        n = int(self.headers.get("Content-Length") or 0)
        left = min(n, MAX_UPLOAD + 1)
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            left -= len(chunk)

    def _auth_ok(self):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:], validate=True).decode("utf-8")
        except Exception:
            return False
        user, sep, pw = raw.partition(":")
        if not sep:
            return False
        # On bytes: compare_digest raises TypeError on non ASCII strings, which would
        # drop the connection and turn a wrong password into a crash.
        return (hmac.compare_digest(user.encode("utf-8"), b"translator")
                and hmac.compare_digest(pw.encode("utf-8"), get_password().encode("utf-8")))

    def _csrf_ok(self):
        """A state changing request needs our own header, which a browser will not
        add from another origin without a preflight, plus an Origin check when one
        is present."""
        if self.command != "POST":
            return True
        if self.headers.get("X-XLIFF-Edit") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            try:
                oh = urllib.parse.urlparse(origin).netloc
            except ValueError:
                return False
            if oh != host:
                return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("file too large")
        buf = b""
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) < n:
            raise ValueError("upload truncated: received %d of %d bytes" % (len(buf), n))
        return buf

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    @staticmethod
    def _disposition(name):
        """Content-Disposition dla nazw z polskimi znakami (RFC 5987)."""
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        ascii_name = re.sub(r'[^\w.\- ]', "_", ascii_name) or "file"
        quoted = urllib.parse.quote(name, safe="")
        return 'attachment; filename="%s"; filename*=UTF-8\'\'%s' % (ascii_name, quoted)

    # ---- routing ----

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        if not self._auth_ok():
            self._drain()
            self._send(401, json.dumps({"error": "authentication required"}),
                       "application/json; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="XLIFF editor"'})
            return
        if not self._csrf_ok():
            self._drain()
            self._err(403, "missing X-XLIFF-Edit header, or a foreign origin")
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            self._dispatch(method, path)
        except KeyError as e:
            self._err(404, str(e).strip("'"))
        except ValueError as e:
            self._err(400, str(e))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._err(500, "%s" % type(e).__name__)

    def _dispatch(self, method, path):
        if method == "GET" and path in ("/", "/index.html"):
            return self._static("ui.html", "text/html; charset=utf-8")
        if method == "GET" and path == "/ui.js":
            return self._static("ui.js", "text/javascript; charset=utf-8")
        if method == "GET" and path == "/ui.css":
            return self._static("ui.css", "text/css; charset=utf-8")
        if method == "GET" and path == "/api/health":
            return self._json({"ok": True})
        if method == "GET" and path == "/api/files":
            return self._json({"files": self._list_files()})
        if method == "POST" and path == "/api/upload":
            return self._upload()
        if method == "POST" and path == "/api/glossary":
            return self._glossary_upload()
        if method == "GET" and path == "/api/glossary":
            return self._json({"terms": self._glossary()})

        m = re.fullmatch(r"/api/file/([0-9a-f]{16})(/[\w.]*)?", path)
        if m:
            return self._file_route(method, m.group(1), m.group(2) or "")
        self._err(404, "unknown path")

    def _static(self, name, ctype):
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            raise KeyError("missing %s" % name)
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- operacje ----

    def _list_files(self):
        out = []
        if not os.path.isdir(DATA):
            return out
        for fid in sorted(os.listdir(DATA)):
            mp = os.path.join(DATA, fid, "meta.json")
            if not os.path.exists(mp):
                continue
            try:
                with open(mp, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue                       # one broken entry must not take the list down
            try:
                meta["edits"] = len(_read_state(os.path.join(DATA, fid)).get("edits", {}))
            except Exception:
                meta["edits"] = 0
            out.append(meta)
        return sorted(out, key=lambda m: m.get("uploaded", 0), reverse=True)

    def _upload(self):
        q = self._query()
        name = _safe_name((q.get("name") or ["file.xlz"])[0])
        blob = self._body()
        if not blob:
            raise ValueError("empty request body")
        pkg = Package(blob, name)
        fid = secrets.token_hex(8)
        d = os.path.join(DATA, fid)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(d, "original.bin"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        meta = {
            "id": fid, "name": name, "size": len(blob), "uploaded": int(time.time()),
            "is_zip": pkg.is_zip, "xlf": pkg.doc.name, "units": len(pkg.doc.units),
            "translatable": sum(1 for u in pkg.doc.units if u["translate"] != "no"),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        fd = os.open(os.path.join(d, "meta.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        with LOCK:
            OPEN_DOCS[fid] = pkg
        self._json(meta)

    def _glossary(self):
        p = os.path.join(DATA, "glossary.json")
        if not os.path.exists(p):
            return []
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _glossary_upload(self):
        q = self._query()
        name = _safe_name((q.get("name") or ["glossary.csv"])[0])
        terms = parse_glossary(self._body(), name)
        if not terms:
            raise ValueError("found no term pairs in that file")
        os.makedirs(DATA, mode=0o700, exist_ok=True)
        with LOCK:
            tmp = os.path.join(DATA, "glossary.%s.tmp" % secrets.token_hex(6))
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(terms, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(DATA, "glossary.json"))
        self._json({"terms": len(terms), "sample": terms[:5]})

    def _file_route(self, method, fid, sub):
        # These two have to work even when the file's saved state is corrupt,
        # because they are the only way out of that situation.
        if method == "POST" and sub == "/reset":
            with LOCK:
                OPEN_DOCS.pop(fid, None)
                sp = os.path.join(_fdir(fid), "state.json")
                if os.path.exists(sp):
                    os.remove(sp)
            return self._json({"ok": True})
        if method == "POST" and sub == "/delete":
            with LOCK:
                OPEN_DOCS.pop(fid, None)
                shutil.rmtree(_fdir(fid), ignore_errors=True)
            return self._json({"ok": True})

        pkg = load_pkg(fid)
        doc = pkg.doc

        if method == "GET" and sub in ("", "/"):
            q = self._query()
            after = (q.get("after") or [""])[0]
            try:
                lim = min(max(int((q.get("limit") or ["200"])[0]), 1), 2000)
            except ValueError:
                raise ValueError("limit must be a number")
            flt = (q.get("filter") or ["all"])[0]
            needle = (q.get("q") or [""])[0].lower()
            units = doc.units
            if flt == "translatable":
                units = [u for u in units if u["translate"] != "no"]
            elif flt == "untranslated":
                units = [u for u in units if u["translate"] != "no"
                         and not plain(u["tgt_tokens"]).strip()]
            elif flt == "edited":
                units = [u for u in units if u["dirty"]]
            if needle:
                units = [u for u in units
                         if needle in plain(u["src_tokens"]).lower()
                         or needle in plain(u["tgt_tokens"]).lower()
                         or needle == u["id"].lower()
                         or needle == u["key"]]
            total = len(units)
            target_language = getattr(doc, "target_language", "") or ""
            # Cursor paging by segment number, which survives an edit pushing a
            # segment out of the active filter. Offset paging lost segments silently.
            if after != "":
                try:
                    a = int(after)
                except ValueError:
                    raise ValueError("bad cursor")
                units = [u for u in units if int(u["key"]) > a]
            page = units[:lim]
            return self._json({
                "total": total, "units": [unit_payload(u) for u in page],
                "next": page[-1]["key"] if len(units) > lim else None,
                "stats": self._stats(doc),
                "source_language": getattr(doc, "source_language", "") or "",
                "target_language": target_language,
            })

        if method == "POST" and sub == "/segment":
            data = json.loads(self._body() or b"{}")
            with LOCK:
                key = str(data.get("key", data.get("id", "")))
                u = doc.unit(key)
                if u["translate"] == "no" and not data.get("force"):
                    raise ValueError("segment is marked translate=no")
                if "tokens" in data:
                    pool = {t["sref"]: t for t in u["tgt_tokens"] if t["kind"] == "tag"}
                    pool.update({t["sref"]: t for t in u["src_tokens"] if t["kind"] == "tag"})
                    toks = []
                    for t in data["tokens"]:
                        if t.get("kind") == "tag":
                            ref = t.get("ref")
                            if ref not in pool:
                                raise ValueError("unknown tag %r" % (ref,))
                            toks.append(pool[ref])
                        else:
                            toks.append({"kind": "text", "text": str(t.get("text", ""))})
                    doc.set_target_tokens(key, toks)
                elif "text" in data:
                    doc.set_target_text(key, str(data["text"]))
                if data.get("state"):
                    doc.set_state(key, str(data["state"]))
                save_state(fid, pkg)
                return self._json(unit_payload(doc.unit(key)))

        if method == "POST" and sub == "/replace":
            data = json.loads(self._body() or b"{}")
            find = str(data.get("find", ""))
            repl = str(data.get("replace", ""))
            if not find:
                raise ValueError("empty search pattern")
            pat = re.escape(find)
            if data.get("whole"):
                pat = r"(?<!\w)%s(?!\w)" % pat
            rx = re.compile(pat, 0 if data.get("case") else re.IGNORECASE)
            dry = bool(data.get("dry"))
            with LOCK:
                changed = []
                for u in doc.units:
                    if u["translate"] == "no":
                        continue
                    hit = False
                    new_toks = []
                    for t in u["tgt_tokens"]:
                        if t["kind"] == "text" and rx.search(t["text"]):
                            hit = True
                            # A lambda keeps the replacement literal, so a backslash sequence in the
                            # user's replacement text is not read as a regex template.
                            new_toks.append({"kind": "text", "text": rx.sub(lambda m: repl, t["text"])})
                        else:
                            new_toks.append(t)
                    if hit:
                        changed.append({"key": u["key"], "id": u["id"],
                                        "before": plain(u["tgt_tokens"]),
                                        "after": plain(new_toks)})
                        if not dry:
                            doc.set_target_tokens(u["key"], new_toks)
                if not dry:
                    save_state(fid, pkg)
            return self._json({"count": len(changed), "changes": changed[:500], "dry": dry})

        if method == "GET" and sub == "/qa":
            issues = qa_check(doc, glossary=self._glossary())
            summary = {}
            for i in issues:
                summary[i["rule"]] = summary.get(i["rule"], 0) + 1
            return self._json({"count": len(issues), "summary": summary,
                               "issues": issues[:QA_CAP],
                               "truncated": max(0, len(issues) - QA_CAP)})

        if method == "GET" and sub == "/diff":
            out = [{"key": u["key"], "id": u["id"],
                    "before": plain(tokenize(u["orig_tgt_inner"])),
                    "after": plain(u["tgt_tokens"])}
                   for u in doc.units if u["dirty"]]
            return self._json({"count": len(out), "changes": out})

        if method == "GET" and sub == "/download":
            blob = pkg.to_bytes()
            with open(os.path.join(_fdir(fid), "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            return self._send(200, blob, "application/octet-stream",
                              {"Content-Disposition": self._disposition(meta["name"]),
                               "X-Sha256": hashlib.sha256(blob).hexdigest()})

        if method == "GET" and sub == "/export.csv":
            rows = ["key\tid\ttranslate\tstate\tsource\ttarget"]
            clean = lambda s: s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            for u in doc.units:
                rows.append("\t".join([u["key"], u["id"], u["translate"], u["tgt_state"] or "",
                                       clean(plain(u["src_tokens"])),
                                       clean(plain(u["tgt_tokens"]))]))
            return self._send(200, "\n".join(rows), "text/tab-separated-values; charset=utf-8",
                              {"Content-Disposition": self._disposition("segments.tsv")})

        if method == "GET" and sub == "/export.html":
            parts = ["<!doctype html><meta charset=utf-8><title>Bilingual review</title>",
                     "<style>body{font:14px/1.5 system-ui;margin:2rem}",
                     "table{border-collapse:collapse;width:100%}",
                     "td,th{border:1px solid #ccc;padding:.4rem;vertical-align:top}",
                     "tr.ed{background:#fff8e1}</style><table>",
                     "<tr><th>ID<th>Source<th>Target"]
            for u in doc.units:
                if u["translate"] == "no":
                    continue
                parts.append("<tr class='%s'><td>%s<td>%s<td>%s" % (
                    "ed" if u["dirty"] else "", html.escape(u["id"]),
                    html.escape(plain(u["src_tokens"])), html.escape(plain(u["tgt_tokens"]))))
            parts.append("</table>")
            return self._send(200, "".join(parts), "text/html; charset=utf-8")

        raise KeyError("unknown operation %s" % sub)

    @staticmethod
    def _stats(doc):
        tr = [u for u in doc.units if u["translate"] != "no"]
        return {
            "units": len(doc.units),
            "translatable": len(tr),
            "translated": sum(1 for u in tr if plain(u["tgt_tokens"]).strip()),
            "edited": sum(1 for u in doc.units if u["dirty"]),
        }


def make_server(host="127.0.0.1", port=8070):
    """Build the server without serving, so a test can run it in process."""
    os.makedirs(DATA, mode=0o700, exist_ok=True)
    os.chmod(DATA, 0o700)
    get_password()
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    global DATA
    import argparse
    parser = argparse.ArgumentParser(
        prog="xliff-safe-edit",
        description="Edit an XLIFF delivery in a browser without re-serialising it.")
    parser.add_argument("--host", default=os.environ.get("XE_HOST", "127.0.0.1"),
                        help="interface to bind (default: 127.0.0.1, local only)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("XE_PORT", "8070")))
    parser.add_argument("--data", default=None,
                        help="state directory (default: %s)" % DATA)
    args = parser.parse_args(argv)
    if args.data:
        DATA = os.path.abspath(args.data)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: binding %s exposes a single user tool with one shared "
              "password to the network. Forward the port instead." % args.host,
              file=sys.stderr)
    server = make_server(args.host, args.port)
    print("XLIFF editor on http://%s:%d   user: translator   password: %s"
          % (args.host, args.port, os.path.join(DATA, "secret")), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", file=sys.stderr)
        server.shutdown()
    return 0


if __name__ == "__main__":
    main()
