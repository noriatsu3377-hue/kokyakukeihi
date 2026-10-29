#!/usr/bin/env python3
"""公式LINE（Lメッセージ）の予約データを顧客管理システム用に書き出す。

使い方: python3 tools/sync_line_bookings.py <get_salon_bookings の結果JSON> [--calendar <list_events の結果JSON>]

- Googleカレンダーの予定は、お客様の予約らしいものだけを取り込む（Lメッセージが自動で作った予定は、LINE予約との照合用）
- line-drafts/visits.json があれば「当日の連絡（実施メニュー・支払い方法）」として一緒に渡す

- line-bookings.js      … このMac用（平文。.gitignore 済みでGitHubには上がらない）
- line-bookings.enc.js  … スマホ／GitHub Pages用（合言葉で暗号化。GitHubに上げる）

合言葉は macOS キーチェーン（項目名: crm-sync-passphrase）から読むだけで、画面やファイルには出さない。
合言葉が未登録なら暗号化ファイルは作らない。予約内容が前回と同じなら push しない。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN = os.path.join(ROOT, "line-bookings.js")
ENC = os.path.join(ROOT, "line-bookings.enc.js")
STATE = os.path.join(ROOT, "line-drafts", ".sync_state")
KEYCHAIN_ITEM = "crm-sync-passphrase"
ITER = 200000  # index.html の SYNC_PBKDF2_ITER と揃える
VISITS = os.path.join(ROOT, "line-drafts", "visits.json")
CUSTOMER_UPDATES = os.path.join(ROOT, "line-drafts", "customer-updates.json")  # 顧客情報・カルテの連絡
JST = timezone(timedelta(hours=9))
# お客様の予約らしい予定（タイトル）と、除外する予定
BOOKING_RE = re.compile(r"施術|セッション|新規|リピート|体験|様|ストレッチ|整体|パーソナル")
EXCLUDE_RE = re.compile(r"経絡|セミナー|勉強会|打ち合わせ|打合せ|歯医者|タトゥー|ZERO|零式-|調整法|合宿|講習|研修|zoom|Zoom|Train|会議")


def normalize(raw):
    out = []
    for b in raw.get("bookings", []):
        out.append({
            "id": b.get("id"),
            "lmFriendId": b.get("lmessage_friend_id"),
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


def local_dt(v):
    """Googleカレンダーの日時を日本時間の 'YYYY-MM-DDTHH:MM' にそろえる"""
    if not v:
        return ""
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(JST).strftime("%Y-%m-%dT%H:%M")


def normalize_calendar(raw):
    out = []
    for e in raw.get("events", []) or []:
        if e.get("status") == "cancelled" or e.get("eventType") not in (None, "DEFAULT"):
            continue
        st = (e.get("start") or {}).get("dateTime")
        if not st:  # 終日の予定は対象外
            continue
        summary = e.get("summary") or ""
        desc = e.get("description") or ""
        ev = {"eventId": e.get("id"), "start": local_dt(st), "end": local_dt((e.get("end") or {}).get("dateTime")), "summary": summary}
        m = re.search(r"friendlist/my_page/(\d+)", desc)
        if m:  # Lメッセージが自動で作った予定 → LINE予約との照合だけに使う
            o = re.search(r"予約日時</b><br>(\d{4})年(\d{2})月(\d{2})日\s*(\d{1,2}:\d{2})", desc)
            ev.update(kind="line", lmFriendId=int(m.group(1)),
                      origDate=f"{o.group(1)}-{o.group(2)}-{o.group(3)}" if o else "", origStart=o.group(4).zfill(5) if o else "")
        elif (BOOKING_RE.search(summary) and not EXCLUDE_RE.search(summary)
              and datetime.fromisoformat(ev["end"] or ev["start"]) - datetime.fromisoformat(ev["start"]) <= timedelta(hours=3)):  # 3時間超はセミナー等とみなす
            ev.update(kind="manual", note=re.sub(r"<[^>]+>", " ", desc).strip()[:300])
        else:
            continue
        out.append(ev)
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

    calendar, window = [], None
    if "--calendar" in sys.argv:
        with open(sys.argv[sys.argv.index("--calendar") + 1], encoding="utf-8") as f:
            craw = json.load(f)
        calendar = normalize_calendar(craw)
        window = craw.get("window")  # {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}（取得した期間）
    visits = []
    if os.path.exists(VISITS):
        with open(VISITS, encoding="utf-8") as f:
            visits = json.load(f)

    customer_updates = []
    if os.path.exists(CUSTOMER_UPDATES):
        with open(CUSTOMER_UPDATES, encoding="utf-8") as f:
            customer_updates = json.load(f)

    payload = {"syncedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "salonId": 12279, "bookings": bookings,
               "calendar": calendar, "calendarWindow": window, "visits": visits,
               "customerUpdates": customer_updates}
    body = json.dumps(payload, ensure_ascii=False)
    with open(PLAIN, "w", encoding="utf-8") as f:
        f.write("// 公式LINE（Lメッセージ）予約の同期データ。個人情報を含むためGitHubには上げないこと。\n")
        f.write("window.LINE_BOOKINGS_SYNC = " + body + ";\n")
    print(f"このMac用に書き出しました: LINE予約 {len(bookings)}件 / カレンダー {len(calendar)}件 / 当日の連絡 {len(visits)}件 / 顧客情報の連絡 {len(customer_updates)}件")

    digest = hashlib.sha256(json.dumps([bookings, calendar, window, visits, customer_updates], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    prev = open(STATE).read().strip() if os.path.exists(STATE) else ""
    if digest == prev:
        print("予約内容に変更なし → GitHubへの反映はスキップ")
        return

    if os.environ.get("CRM_SYNC_NO_PUSH"):  # テスト用：GitHubには反映しない
        print("テストモードのため、GitHubへの反映はスキップ")
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
