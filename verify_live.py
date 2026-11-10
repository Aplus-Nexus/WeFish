"""Safe live-account smoke check for a local WeFish installation.

The command intentionally prints only booleans, counts and public metadata. It
never prints cookies, API keys, session headers, notes, or chapter contents.
"""
from __future__ import annotations

import argparse
import sys
import threading

from wefish_core import AuthError, WeReadClient, WeFishError, apply_environment_credentials


def _has_web_cookie(client: WeReadClient) -> bool:
    values = {cookie.name: cookie.value for cookie in client.session.cookies}
    return bool(len(values.get("wr_skey", "")) >= 8 or len(values.get("wr_gid", "")) >= 5)


def _restore_with_timeout(client: WeReadClient, timeout: float):
    """Restore through a temporary client without hanging the diagnostic."""
    result = {}
    done = threading.Event()

    def restore():
        try:
            restored = WeReadClient(timeout=client.timeout, vault=client.vault)
            result["restored"] = restored.restore_credentials()
            result["client"] = restored
        except Exception as exc:  # noqa: BLE001 - diagnostic must report and exit
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=restore, name="wefish-live-credential-restore", daemon=True).start()
    if not done.wait(max(0.1, float(timeout))):
        return "busy"
    if result.get("error") is not None:
        return "error"
    restored = result.get("client")
    if restored is not None:
        client.session = restored.session
        client.web_opener = restored.web_opener
        client.qr_cookies = restored.qr_cookies
        client.qr_opener = restored.qr_opener
    return bool(result.get("restored"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check a local WeFish login without printing secrets")
    parser.add_argument("--reader-url", help="optional weread.qq.com /web/reader/ or /web/mp/reader/ link")
    parser.add_argument("--restore-timeout", type=float, default=5.0, help="maximum seconds to wait for the OS credential store")
    args = parser.parse_args(argv)

    client = WeReadClient()
    environment_credentials = apply_environment_credentials(client)
    restored = True if environment_credentials else _restore_with_timeout(client, args.restore_timeout)
    if restored == "busy":
        print("credential_store=busy")
        print("web_cookie=unknown")
        print("gateway_api_key=unknown")
        print("reader=skipped reason=credential store did not respond")
        return 2
    has_key = bool(client.session.api_key)
    has_cookie = _has_web_cookie(client)
    store_status = 'environment' if environment_credentials else 'error' if restored == 'error' else 'restored' if restored else 'empty'
    print(f"credential_store={store_status}")
    print(f"web_cookie={'ok' if has_cookie else 'missing'}")
    print(f"gateway_api_key={'ok' if has_key else 'missing'}")

    if has_key:
        try:
            shelf = client.shelf()
            books = shelf.get("books", []) if isinstance(shelf, dict) else []
            print(f"shelf=ok books={len(books)}")
        except (AuthError, WeFishError) as exc:
            print(f"shelf=failed reason={str(exc)[:160]}")
    else:
        print("shelf=skipped reason=Skill API Key missing")

    if has_cookie:
        try:
            account = client.verify_web_session()
            name = str(account.get("name") or account.get("nickName") or "unknown")
            print(f"web_session=ok account={name[:80]}")
        except (AuthError, WeFishError) as exc:
            print(f"web_session=failed reason={str(exc)[:160]}")
            if not args.reader_url:
                return 1

    if args.reader_url:
        if not has_cookie:
            print("reader=skipped reason=Web Reader Cookie missing")
            return 1
        try:
            book = client.import_reader_book(args.reader_url)
            if book.get("isMp"):
                articles = book.get("articles", [])
                print(f"reader=ok kind=mp title={book.get('title', '')} articles={len(articles)}")
                if articles:
                    body = client.mp_content(articles[0].get("reviewId", ""), inline_images=False)
                    print(f"reader_content=ok chars={len(body)}")
            else:
                chapters = book.get("chapters", [])
                print(f"reader=ok kind=book title={book.get('title', '')} chapters={len(chapters)}")
                current = book.get("chapterUid")
                if current is not None:
                    try:
                        membership = client.reader_membership(book["bookId"], current)
                        remaining = membership.get("remainingSeconds")
                        remaining_days = int((int(remaining) + 86399) // 86400) if remaining is not None else 0
                        print(
                            f"membership={membership.get('status', 'unknown')}"
                            f" snapshot={'yes' if membership.get('hasSnapshot') else 'no'}"
                            f" remaining_days={remaining_days}"
                        )
                    except (AuthError, WeFishError, ValueError) as exc:
                        print(f"membership=failed reason={str(exc)[:160]}")
                    body = client.fetch_chapter(book["bookId"], current)
                    print(f"reader_content=ok chars={len(body)}")
                    if has_key:
                        try:
                            client.progress(book["bookId"])
                            print("cloud_progress=ok")
                        except (AuthError, WeFishError) as exc:
                            print(f"cloud_progress=failed reason={str(exc)[:160]}")
                        try:
                            client.read_stats("monthly")
                            client.notebooks()
                            print("reading_data=ok")
                        except (AuthError, WeFishError) as exc:
                            print(f"reading_data=failed reason={str(exc)[:160]}")
                    else:
                        print("cloud_progress=skipped reason=Skill API Key missing")
                        print("reading_data=skipped reason=Skill API Key missing")
        except (AuthError, WeFishError, ValueError) as exc:
            print(f"reader=failed reason={str(exc)[:160]}")
            return 1
    elif has_cookie:
        print("reader=ready reason=pass --reader-url to verify a book")
    else:
        print("reader=skipped reason=Web Reader Cookie missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
