"""Read-only X timelines and role identity checks; no write methods or permissions."""

import re
import unicodedata
from urllib.parse import urlencode, urlsplit

from pipmind.contracts import loads, stamp, utc
from pipmind.feeds.common import FeedError, instant, json_body, markup
from pipmind.feeds.macro import calendar_date
from pipmind.feeds.news import parse_feed


def name(value):
    return " ".join(re.sub(r"[^\w ]", " ", unicodedata.normalize("NFKC", value)).casefold().split())


class XReads:
    def __init__(self, reads, token, *, store=None, now=None):
        if not token:
            raise FeedError("missing_x_read_token")
        self.reads, self._token = reads, token
        self.store, self.now = store, now

    def get(self, path, query):
        if not (path == "/2/users/by" or re.fullmatch(r"/2/users/\d{1,19}/tweets", path)):
            raise FeedError("x_endpoint_forbidden")
        if self.store and self.store.state("x:backoff", 0) > self.now().timestamp():
            raise FeedError("x_rate_limited")
        response = self.reads.get(
            "https://api.x.com" + path + "?" + urlencode(query),
            headers={"Authorization": "Bearer " + self._token},
        )
        if response.status == 429 and self.store:
            try:
                delay = min(86400, max(60, float(response.headers.get("Retry-After", 900))))
            except ValueError:
                delay = 900
            self.store.save("x:backoff", self.now().timestamp() + delay)
        data = json_body(response)
        if not isinstance(data, dict) or data.get("errors"):
            raise FeedError("partial_x_response")
        return data

    def users(self, handles):
        return self.get(
            "/2/users/by",
            {
                "usernames": ",".join(handles),
                "user.fields": "name,description,verified,verified_type,parody,protected",
            },
        ).get("data", [])

    def timeline(self, user_id, *, since=None, token=None):
        query = {
            "max_results": 100,
            "exclude": "replies,retweets",
            "post.fields": "created_at,author_id,referenced_posts,note_post",
        }
        if since:
            query["since_id"] = since
        if token:
            query["pagination_token"] = token
        return self.get(f"/2/users/{user_id}/tweets", query)


def holder_lookup(reads, recipe):
    response = reads.get(recipe["url"])
    if response.status != 200:
        raise FeedError("holder_lookup_failed")
    try:
        value = response.body.decode("utf-8")
        if recipe.get("json_path"):
            value = loads(value)
            for key in recipe["json_path"]:
                value = value[key]
        clean, links = markup(value)
        start, end = recipe["start"], recipe["end"]
        if clean.count(start) != 1:
            raise ValueError
        candidate = clean.split(start, 1)[1].split(end, 1)
        if len(candidate) != 2 or not 2 <= len(candidate[0].strip()) <= 120:
            raise ValueError
        return candidate[0].strip(), links
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise FeedError("holder_parse_failed") from None


def verified(user, holder, role, *, institutional=False):
    return (
        isinstance(user, dict)
        and re.fullmatch(r"\d{1,19}", str(user.get("id", "")))
        and user.get("protected") is False
        and user.get("parody") is False
        and user.get("verified") is True
        and isinstance(user.get("name"), str)
        and isinstance(user.get("description"), str)
        and user.get("verified_type")
        in ({"government", "business"} if institutional else {"blue", "government"})
        and (institutional or name(user.get("name", "")) == name(holder))
        and all(
            word.casefold() in user.get("description", "").casefold()
            for word in role.get("institutional_words" if institutional else "role_words", [])
        )
    )


class Social:
    def __init__(self, store, reads, x, roles, calendar, now):
        self.store, self.reads, self.x = store, reads, x
        self.roles, self.calendar, self.now = roles, calendar, now

    def state(self, role):
        return self.store.state(
            "role:" + role["id"],
            {"holder": role.get("holder"), "accounts": role["accounts"], "checked_at": None},
        )

    def refresh(self, role):
        old, now = self.state(role), self.now()
        holder, links = old["holder"], []
        if role.get("lookups"):
            for recipe in role["lookups"]:
                try:
                    holder, links = holder_lookup(self.reads, recipe)
                    break
                except FeedError:
                    self.store.alert("social", "holder_lookup_failed", now)
            else:
                return False  # No state mutation on failed primary and fallback.
        accounts = [dict(a) for a in old["accounts"] if a["kind"] == "institutional"]
        changed = name(holder or "") != name(old["holder"] or "")
        if not changed:
            accounts.extend(dict(a) for a in old["accounts"] if a["kind"] != "institutional")
        else:
            # Only links read from the configured official profile page can nominate a handle.
            try:
                profile = self.reads.get(role["profile_url"])
                if profile.status != 200:
                    raise FeedError("personal_profile_unavailable")
                profile_text, official_links = markup(profile.body.decode("utf-8"))
                if name(holder) not in name(profile_text):
                    raise FeedError("personal_profile_unverified")
                candidates = set()
                for link in official_links:
                    parts = urlsplit(link)
                    handle = parts.path.strip("/")
                    if (
                        parts.scheme == "https"
                        and parts.netloc in {"x.com", "twitter.com"}
                        and re.fullmatch(r"\w{1,15}", handle)
                    ):
                        candidates.add(handle)
                candidates = sorted(candidates)
                users = self.x.users(candidates) if candidates else []
                choices = [
                    user
                    for user in users
                    if verified(user, holder, role)
                    and user["username"].casefold() in {c.casefold() for c in candidates}
                ]
                if len(choices) > 1:
                    raise FeedError("ambiguous_personal_account")
                if choices:
                    accounts.append(
                        {
                            "handle": choices[0]["username"],
                            "kind": "personal",
                            "poll": "full_timeline",
                            "user_id": choices[0]["id"],
                        }
                    )
                else:
                    self.store.alert("social", "personal_account_unverified", now)
            except (FeedError, UnicodeError, KeyError, TypeError, AttributeError):
                self.store.alert("social", "personal_account_unverified", now)
        # Verify every selected handle; preserve previous role state if account reads fail.
        try:
            users = self.x.users([a["handle"] for a in accounts]) if accounts else []
            by_handle = {u["username"].casefold(): u for u in users}
            for account in accounts:
                user = by_handle.get(account["handle"].casefold())
                if not verified(
                    user, holder, role, institutional=account["kind"] == "institutional"
                ):
                    account["unverified"] = True
                    self.store.alert("social", "account_identity_unverified", now)
                else:
                    account["user_id"], account["unverified"] = user["id"], False
            self.store.save(
                "role:" + role["id"],
                {"holder": holder, "accounts": accounts, "checked_at": stamp(now)},
            )
            return True
        except (FeedError, KeyError, TypeError, AttributeError):
            self.store.alert("social", "account_lookup_failed", now)
            if changed:
                self.store.save(
                    "role:" + role["id"],
                    {
                        "holder": holder,
                        "accounts": [
                            {**a, "unverified": True}
                            for a in accounts
                            if a["kind"] == "institutional"
                        ],
                        "checked_at": stamp(now),
                    },
                )
            return False

    def eligible(self, role, account, now):
        if role["priority"] > 2 or account.get("dormant") or account.get("unverified"):
            return False
        if now.weekday() >= 5 and not (now.weekday() == 6 and 16 <= now.hour < 20):
            return False
        policy = account["poll"]
        if policy == "rss_only":
            return False
        if policy == "backup" and not account.get("primary_down", False):
            return False
        if policy == "event_window":
            times = [
                calendar_date(r["date"])
                for r in self.calendar.rows()
                if r["impact"].lower() == "high"
            ]
            return any(-300 <= (now - event).total_seconds() <= 1800 for event in times)
        return True

    def poll(self, role, account):
        now, user_id = self.now(), account.get("user_id")
        if not user_id:
            raise FeedError("x_account_not_resolved")
        key = "x:cursor:" + user_id
        progress = self.store.state(key, {"since": None, "next": None, "newest": None, "seen": []})
        for _ in range(5):  # Continue remaining pages on the next tick, without advancing since_id.
            page = self.x.timeline(user_id, since=progress["since"], token=progress["next"])
            if page.get("errors") or not isinstance(page.get("data", []), list):
                raise FeedError("partial_x_response")
            payloads, newest, latest = (
                [],
                progress["newest"] or progress["since"],
                self.store.state("x:last_post:" + user_id),
            )
            for post in page.get("data", []):
                identifier = post["id"]
                if not re.fullmatch(r"\d{1,19}", identifier) or post["author_id"] != user_id:
                    raise FeedError("invalid_x_post_identity")
                when = instant(post["created_at"])
                if when > now:
                    raise FeedError("future_x_post")
                newest = str(max(int(newest or 0), int(identifier)))
                latest = stamp(max(utc(latest), when)) if latest else stamp(when)
                if any(
                    ref["type"] in {"replied_to", "retweeted"}
                    for ref in post.get("referenced_posts", post.get("referenced_tweets", []))
                ):
                    continue
                payloads.append(
                    {
                        "schema_version": 1,
                        "event_id": "x:" + identifier,
                        "author": account["handle"],
                        "handle": account["handle"],
                        "role": role["id"],
                        "text": post.get("note_post", post.get("note_tweet", {})).get(
                            "text", post["text"]
                        ),
                        "ts": stamp(when),
                        "url": "https://x.com/" + account["handle"] + "/status/" + identifier,
                        "received_at": stamp(now),
                    }
                )
            token = page.get("meta", {}).get("next_token")
            if token and (token in progress["seen"] or len(progress["seen"]) >= 1000):
                raise FeedError("x_pagination_loop")
            progress = (
                {**progress, "next": token, "newest": newest, "seen": progress["seen"] + [token]}
                if token
                else {
                    "since": newest or progress["since"],
                    "next": None,
                    "newest": None,
                    "seen": [],
                }
            )
            with self.store.transaction() as db:
                for payload in sorted(payloads, key=lambda p: (p["ts"], p["event_id"])):
                    self.store.emit(db, "social", payload, now)
                self.store.save(key, progress, db=db)
                self.store.save("x:last_post:" + user_id, latest, db=db)
            if not token:
                break

    def step(self):
        now, selected = self.now(), set()
        for role in sorted(self.roles, key=lambda r: (r["priority"], r["id"])):
            if role.get("primary_feed_url") and self.store.claim(
                "primary:poll:" + role["id"], now, 300
            ):
                try:
                    response = self.reads.get(role["primary_feed_url"])
                    if response.status != 200:
                        raise FeedError("primary_social_read_failed")
                    entries = parse_feed(
                        response.body,
                        {
                            "id": role["id"],
                            "kind": "breaking",
                            "allow_summary": role.get("primary_allow_summary", False),
                        },
                        now,
                    )
                    payloads = [
                        {
                            "schema_version": 1,
                            "event_id": "mirror:" + entry["event_id"],
                            "author": role.get("primary_author", role["id"]),
                            "role": role["id"],
                            "text": entry.get("summary") or entry["headline"],
                            "ts": entry["published_at"],
                            "url": entry["url"],
                            "received_at": stamp(now),
                        }
                        for entry in entries
                    ]
                    newest = max((entry["published_at"] for entry in entries), default=None)
                    self.store.page(
                        "social",
                        payloads,
                        "primary:status:" + role["id"],
                        {"failed": False, "latest": newest},
                        now,
                    )
                except ValueError:
                    self.store.alert("social", "primary_social_failed", now)
                    self.store.save(
                        "primary:status:" + role["id"], {"failed": True, "latest": None}
                    )
            if self.x is None:
                continue
            saved = self.state(role)
            weekly = now.weekday() == 6 and 16 <= now.hour < 20
            if (saved["checked_at"] is None or weekly) and self.store.claim(
                "role:check:" + role["id"], now, 86400 if saved["checked_at"] is None else 6 * 86400
            ):
                self.refresh(role)
                refreshed = self.state(role)
                for account in refreshed["accounts"]:
                    if (
                        account["poll"] == "rss_only"
                        or account.get("unverified")
                        or role["priority"] > 2
                    ):
                        continue
                    try:
                        self.poll(role, account)
                        recent = self.store.state("x:last_post:" + account["user_id"])
                        cursor = self.store.state("x:cursor:" + account["user_id"], {})
                        if not cursor.get("next"):
                            account["dormant"] = (
                                recent is None or (now - utc(recent)).total_seconds() > 30 * 86400
                            )
                    except (FeedError, KeyError, TypeError, ValueError):
                        self.store.alert("social", "account_activity_check_failed", now)
                self.store.save("role:" + role["id"], refreshed)
            saved = self.state(role)
            for account in saved["accounts"]:
                if account["poll"] == "backup":
                    primary = self.store.state("primary:status:" + role["id"])
                    account = {
                        **account,
                        "primary_down": bool(
                            primary
                            and (
                                primary["failed"]
                                or (
                                    primary["latest"]
                                    and (now - utc(primary["latest"])).total_seconds()
                                    > role.get("primary_quiet_seconds", 86400)
                                )
                            )
                        ),
                    }
                if (
                    not self.eligible(role, account, now)
                    or account["handle"].casefold() in selected
                ):
                    continue
                selected.add(account["handle"].casefold())
                interval = (120 if 7 <= now.hour < 21 else 600) if role["priority"] == 1 else 900
                if not self.store.claim("x:poll:" + account["handle"].casefold(), now, interval):
                    continue
                try:
                    self.poll(role, account)
                except (FeedError, KeyError, TypeError, ValueError):
                    self.store.alert("social", "x_poll_failed", now)
