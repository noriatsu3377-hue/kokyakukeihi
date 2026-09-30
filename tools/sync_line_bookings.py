#!/usr/bin/env python3
"""公式LINE（Lメッセージ）の予約データを顧客管理システム用に書き出す。

使い方: python3 tools/sync_line_bookings.py <get_salon_bookings の結果JSON>

- line-bookings.js      … このMac用（平文。.gitignore 済みでGitHubには上がらない）
- line-bookings.enc.js  … スマホ／GitHub Pages用（合言葉で暗号化。GitHubに上げる）

合言葉は macOS キーチェーン（項目名: crm-sync-passphrase）から読むだけで、画面やファイルには出さない。
合言葉が未登録なら暗号化ファイルは作らない。予約内容が前回と同じなら push しない。
"""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(ROOT, "line-bookings.js")
ENC = os.path.join(ROOT, "line-bookings.enc.js")
STATE = os.path.join(ROOT, "line-drafts", ".sync_state")
KEYCHAIN_ITEM = "crm-sync-passphrase"
ITER = 200000  # index.html の SYNC_PBKDF2_ITER と揃える


def normalize(raw):
    out = []
    for b in raw.get("bookings", []):
        out.append({
            "id": b.get("id"),
            "courseId": b.get("course_id"),
            "courseName": b.get("course_name", ""),
            "dateBooking": b.get("date_booking", ""),
            "friendId": b.get("line_friend_id", ""),
            "userName": b.get("user_name", ""),
            "status": b.get("status", ""),
            "paymentAmount": b.get("payment_amount", 0),
            "answers": {a.get("question", ""): a.get("answer", "") for a in b.get("form_answers", []) or []},
            "createdAt": b.get("created_at", ""),
        })
    return out


def passphrase():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_ITEM, "-w"],
                       capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def encrypt(text, pw):
    env = dict(os.environ, CRM_SYNC_PW=pw)
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITER), "-md", "sha256",
                        "-salt", "-pass", "env:CRM_SYNC_PW", "-base64", "-A"],
                       input=text.encode("utf-8"), capture_output=True, env=env, check=True)
    return r.stdout.decode().strip()


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)


def main():
    if len(sys.argv) < 2:
        sys.exit("使い方: sync_line_bookings.py <予約JSONファイル>")
    with open(sys.argv[1], encoding="utf-8") as f:
        raw = json.load(f)
    bookings = normalize(raw)
    if not bookings and not raw.get("bookings") == []:
        sys.exit("予約データの形式が正しくありません（中止しました）")

    payload = {"syncedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "salonId": 12279, "bookings": bookings}
    body = json.dumps(payload, ensure_ascii=False)
    with open(PLAIN, "w", encoding="utf-8") as f:
        f.write("// 公式LINE（Lメッセージ）予約の同期データ。個人情報を含むためGitHubには上げないこと。\n")
        f.write("window.LINE_BOOKINGS_SYNC = " + body + ";\n")
    print(f"このMac用に書き出しました: {len(bookings)}件")

    digest = hashlib.sha256(json.dumps(bookings, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    prev = open(STATE).read().strip() if os.path.exists(STATE) else ""
    if digest == prev:
        print("予約内容に変更なし → GitHubへの反映はスキップ")
        return

    pw = passphrase()
    if not pw:
        print("合言葉が未登録のため、スマホ用（暗号化）ファイルは作成していません")
        return
    with open(ENC, "w", encoding="utf-8") as f:
        f.write("// 公式LINE予約の同期データ（合言葉で暗号化済み）\n")
        f.write('window.LINE_BOOKINGS_ENC = "' + encrypt(body, pw) + '";\n')

    git("add", "line-bookings.enc.js")
    c = git("commit", "-m", "sync: 公式LINE予約データを更新（暗号化）", "--", "line-bookings.enc.js")
    if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
        sys.exit("コミットに失敗しました: " + c.stderr.strip())
    p = git("push", "origin", "HEAD")
    if p.returncode != 0:
        sys.exit("GitHubへのpushに失敗しました: " + p.stderr.strip())
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        f.write(digest)
    print("スマホ用（暗号化）ファイルをGitHubに反映しました")


if __name__ == "__main__":
    main()
