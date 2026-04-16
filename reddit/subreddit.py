import html
import json
import re
import time

import requests as http_requests

import praw
from praw.models import MoreComments
from prawcore.exceptions import ResponseException

from utils import settings
from utils.ai_methods import sort_by_similarity
from utils.console import print_step, print_substep
from utils.posttextparser import posttextparser
from utils.subreddit import _contains_blocked_words, get_subreddit_undone
from utils.videos import check_done
from utils.voice import sanitize_text


# ---------------------------------------------------------------------------
#  JSON-based Reddit fetcher (no API credentials required)
# ---------------------------------------------------------------------------

JSON_USER_AGENT = "RedditVideoMakerBot/3.4.0 (compatible; bot; +https://github.com/elebumm/RedditVideoMakerBot)"
JSON_REQUEST_DELAY = 6  # seconds between requests (unauthenticated limit: 10 req/min)
JSON_MAX_RETRIES = 3


def _json_get(url: str, params: dict = None) -> dict:
    """
    Make a GET request to a Reddit .json endpoint with proper User-Agent,
    rate limiting, and retry/backoff logic.
    """
    headers = {"User-Agent": JSON_USER_AGENT}
    if params is None:
        params = {}
    params["raw_json"] = 1  # Prevents Reddit from HTML-encoding characters

    last_exception = None
    for attempt in range(JSON_MAX_RETRIES):
        try:
            resp = http_requests.get(url, headers=headers, params=params, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                # Reddit sometimes returns {"error": 404} inside a 200 response
                if isinstance(data, dict) and "error" in data:
                    raise ValueError(f"Reddit returned error inside 200 response: {data}")
                return data

            elif resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                print_substep(
                    f"Rate limited (429). Retrying in {retry_after}s... (attempt {attempt + 1}/{JSON_MAX_RETRIES})",
                    style="yellow",
                )
                time.sleep(retry_after)
                continue

            elif resp.status_code == 403:
                print_substep(
                    "Reddit returned 403 Forbidden. Your IP or User-Agent may be blocked. "
                    "Try again later or use a different network.",
                    style="bold red",
                )
                raise ConnectionError(f"Reddit returned 403 for {url}")

            else:
                raise ConnectionError(
                    f"Reddit returned HTTP {resp.status_code} for {url}: {resp.text[:200]}"
                )

        except http_requests.exceptions.ConnectionError as e:
            last_exception = e
            wait = 2 ** (attempt + 1)
            print_substep(
                f"Connection error. Retrying in {wait}s... (attempt {attempt + 1}/{JSON_MAX_RETRIES})",
                style="yellow",
            )
            time.sleep(wait)
        except http_requests.exceptions.Timeout as e:
            last_exception = e
            wait = 2 ** (attempt + 1)
            print_substep(
                f"Request timed out. Retrying in {wait}s... (attempt {attempt + 1}/{JSON_MAX_RETRIES})",
                style="yellow",
            )
            time.sleep(wait)

    raise ConnectionError(
        f"Failed to fetch {url} after {JSON_MAX_RETRIES} attempts. Last error: {last_exception}"
    )


def _json_already_done(post_id: str) -> bool:
    """Check if a post has already been turned into a video."""
    import os

    videos_path = "./video_creation/data/videos.json"
    if not os.path.exists(videos_path):
        with open(videos_path, "w+") as f:
            json.dump([], f)
        return False
    with open(videos_path, "r", encoding="utf-8") as f:
        done_videos = json.load(f)
    for video in done_videos:
        if video["id"] == post_id:
            return True
    return False


def get_subreddit_threads_json(POST_ID: str):
    """
    Fetch Reddit threads using public .json endpoints instead of PRAW.
    No API credentials (client_id / client_secret) required.
    Only a custom User-Agent header is needed.
    """
    print_substep("Using public JSON endpoints (no API credentials).")

    content = {}
    storymode = settings.config["settings"]["storymode"]
    subreddit_config = settings.config["reddit"]["thread"]["subreddit"]

    # Warn if AI similarity is enabled — not supported in JSON mode
    if settings.config["ai"]["ai_similarity_enabled"]:
        print_substep(
            "Warning: AI similarity sorting is not available without API credentials. "
            "Falling back to hot posts.",
            style="bold yellow",
        )

    # ---- Determine which subreddit to use ----
    if not subreddit_config:
        subreddit_name = input("What subreddit would you like to pull from? ").strip()
        subreddit_name = re.sub(r"^r/", "", subreddit_name) or "askreddit"
    else:
        subreddit_name = subreddit_config
        if str(subreddit_name).casefold().startswith("r/"):
            subreddit_name = subreddit_name[2:]
        print_substep(f"Using subreddit: r/{subreddit_name} from TOML config")

    # ---- Fetch a specific post by ID, or browse hot ----
    submission_data = None

    if POST_ID:
        print_step(f"Fetching specific post: {POST_ID}")
        url = f"https://www.reddit.com/comments/{POST_ID}.json"
        data = _json_get(url)
        # .json for a post returns a list: [post_listing, comments_listing]
        post_children = data[0]["data"]["children"]
        if not post_children:
            print_substep("Post not found or has been deleted.", style="bold red")
            exit()
        submission_data = post_children[0]["data"]
        comments_data = data[1]["data"]["children"]

    elif (
        settings.config["reddit"]["thread"]["post_id"]
        and len(str(settings.config["reddit"]["thread"]["post_id"]).split("+")) == 1
    ):
        specific_id = settings.config["reddit"]["thread"]["post_id"]
        print_step(f"Fetching specific post: {specific_id}")
        url = f"https://www.reddit.com/comments/{specific_id}.json"
        data = _json_get(url)
        post_children = data[0]["data"]["children"]
        if not post_children:
            print_substep("Post not found or has been deleted.", style="bold red")
            exit()
        submission_data = post_children[0]["data"]
        comments_data = data[1]["data"]["children"]

    else:
        # Fetch hot posts and find one that hasn't been done yet
        print_step("Getting subreddit threads...")
        url = f"https://www.reddit.com/r/{subreddit_name}/hot.json"
        data = _json_get(url, params={"limit": 25})
        threads = data["data"]["children"]

        for thread in threads:
            post = thread["data"]

            # Skip already-done posts
            if _json_already_done(post["id"]):
                continue

            # Skip stickied posts
            if post.get("stickied", False):
                print_substep("Skipping pinned post...")
                continue

            # Skip NSFW if not allowed
            if post.get("over_18", False):
                try:
                    if not settings.config["settings"]["allow_nsfw"]:
                        print_substep("NSFW Post Detected. Skipping...")
                        continue
                except AttributeError:
                    print_substep("NSFW settings not defined. Skipping NSFW post...")
                    continue

            # Skip posts with blocked words
            post_text = post.get("title", "") + " " + (post.get("selftext", "") or "")
            if _contains_blocked_words(post_text):
                print_substep("Post contains a blocked word. Skipping...")
                continue

            # Skip posts below minimum comments (unless storymode)
            if (
                post.get("num_comments", 0)
                <= int(settings.config["reddit"]["thread"]["min_comments"])
                and not storymode
            ):
                print_substep(
                    f'Post has under the minimum comments ({settings.config["reddit"]["thread"]["min_comments"]}). Skipping...'
                )
                continue

            # Storymode checks
            if storymode:
                if not post.get("selftext"):
                    print_substep("Story mode but post has no text. Skipping...")
                    continue
                if len(post.get("selftext", "")) > (
                    settings.config["settings"].get("storymode_max_length", 2000)
                ):
                    print_substep(
                        f"Post too long for story mode ({len(post['selftext'])} chars). Skipping..."
                    )
                    continue
                if len(post.get("selftext", "")) < 30:
                    continue
                if not post.get("is_self", False):
                    continue

            # This post passes all filters — use it
            submission_data = post
            break

        if submission_data is None:
            print_substep(
                "All posts have been done or filtered out. Try a different subreddit or wait for new posts.",
                style="bold red",
            )
            exit()

        # Now fetch the full post with comments
        time.sleep(JSON_REQUEST_DELAY)
        post_url = f"https://www.reddit.com{submission_data['permalink']}.json"
        # Clean double slashes just in case
        post_url = post_url.replace(".json.json", ".json")
        full_data = _json_get(post_url)
        submission_data = full_data[0]["data"]["children"][0]["data"]
        comments_data = full_data[1]["data"]["children"]

    # ---- Check if this specific post was already done ----
    if _json_already_done(submission_data["id"]):
        if settings.config["reddit"]["thread"]["post_id"]:
            print_step(
                "You already have done this video but since it was declared specifically "
                "in the config file the program will continue"
            )
        else:
            print_step("This post has already been done. Skipping...")
            exit()

    # ---- Build the content dict (same structure as PRAW path) ----
    permalink = submission_data.get("permalink", "")
    threadurl = f"https://new.reddit.com{permalink}"

    upvotes = submission_data.get("score", 0)
    ratio = submission_data.get("upvote_ratio", 0) * 100
    num_comments = submission_data.get("num_comments", 0)

    print_substep(
        f"Video will be: {submission_data['title']} :thumbsup:", style="bold green"
    )
    print_substep(f"Thread url is: {threadurl} :thumbsup:", style="bold green")
    print_substep(f"Thread has {upvotes} upvotes", style="bold blue")
    print_substep(f"Thread has a upvote ratio of {ratio}%", style="bold blue")
    print_substep(f"Thread has {num_comments} comments", style="bold blue")

    content["thread_url"] = threadurl
    content["thread_title"] = html.unescape(submission_data["title"])
    content["thread_id"] = submission_data["id"]
    content["is_nsfw"] = submission_data.get("over_18", False)
    content["thread_score"] = submission_data.get("score", 0)
    content["thread_num_comments"] = submission_data.get("num_comments", 0)
    content["thread_author"] = submission_data.get("author", "[deleted]")
    content["subreddit_name"] = submission_data.get("subreddit", subreddit_name)
    content["comments"] = []

    if storymode:
        selftext = html.unescape(submission_data.get("selftext", ""))
        selftext_html_raw = submission_data.get("selftext_html", "")
        content["selftext_html"] = html.unescape(selftext_html_raw) if selftext_html_raw else ""
        if settings.config["settings"]["storymodemethod"] == 1:
            content["thread_post"] = posttextparser(selftext)
        else:
            content["thread_post"] = selftext
    else:
        for child in comments_data:
            # Only process actual comments (t1), skip "more" objects
            if child.get("kind") != "t1":
                continue

            comment = child["data"]
            body = html.unescape(comment.get("body", ""))
            body_html_raw = comment.get("body_html", "")
            body_html = html.unescape(body_html_raw) if body_html_raw else ""

            # Skip removed/deleted
            if body in ["[removed]", "[deleted]"]:
                continue

            # Skip blocked words
            if _contains_blocked_words(body):
                continue

            # Skip stickied comments
            if comment.get("stickied", False):
                continue

            # Sanitize and validate
            sanitised = sanitize_text(body)
            if not sanitised or sanitised == " ":
                continue

            # Check comment length bounds
            if len(body) > int(settings.config["reddit"]["thread"]["max_comment_length"]):
                continue
            if len(body) < int(settings.config["reddit"]["thread"]["min_comment_length"]):
                continue

            # Skip comments with no author
            if comment.get("author") is None or comment.get("author") == "[deleted]":
                continue

            content["comments"].append(
                {
                    "comment_body": body,
                    "comment_body_html": body_html,
                    "comment_url": comment.get("permalink", ""),
                    "comment_id": comment.get("id", ""),
                    "comment_author": comment.get("author", "[deleted]"),
                    "comment_score": comment.get("score", 0),
                }
            )

    if not storymode and not content["comments"]:
        print_substep("No valid comments found for this post.", style="bold red")
        exit()

    print_substep("Received subreddit threads successfully.", style="bold green")
    return content


# ---------------------------------------------------------------------------
#  Original PRAW-based Reddit fetcher
# ---------------------------------------------------------------------------


def get_subreddit_threads(POST_ID: str):
    """
    Main entry point. Routes to JSON or PRAW based on whether
    client_id is configured.
    """
    # Check if API credentials are available
    client_id = settings.config["reddit"]["creds"].get("client_id", "").strip()
    client_secret = settings.config["reddit"]["creds"].get("client_secret", "").strip()

    # If both credentials are missing or empty, use JSON mode
    if not client_id or not client_secret:
        print_substep(
            "No Reddit API credentials found. Using public JSON endpoints.", style="bold blue"
        )
        return get_subreddit_threads_json(POST_ID)

    # If one is set but not the other, warn and fall back to JSON
    if bool(client_id) != bool(client_secret):
        print_substep(
            "Only one of client_id/client_secret is set. Both are needed for PRAW. "
            "Falling back to public JSON endpoints.",
            style="bold yellow",
        )
        return get_subreddit_threads_json(POST_ID)

    # Validate credential lengths before passing to PRAW
    if len(client_id) < 12 or len(client_secret) < 20:
        print_substep(
            "client_id or client_secret appears too short. "
            "Falling back to public JSON endpoints.",
            style="bold yellow",
        )
        return get_subreddit_threads_json(POST_ID)

    # --- Original PRAW path below (unchanged) ---
    return _get_subreddit_threads_praw(POST_ID)


def _get_subreddit_threads_praw(POST_ID: str):
    """
    Original PRAW-based implementation. Requires valid client_id,
    client_secret, username, and password.
    """
    print_substep("Logging into Reddit via PRAW.")

    content = {}
    if settings.config["reddit"]["creds"]["2fa"]:
        print("\nEnter your two-factor authentication code from your authenticator app.\n")
        code = input("> ")
        print()
        pw = settings.config["reddit"]["creds"]["password"]
        passkey = f"{pw}:{code}"
    else:
        passkey = settings.config["reddit"]["creds"]["password"]
    username = settings.config["reddit"]["creds"]["username"]
    if str(username).casefold().startswith("u/"):
        username = username[2:]
    try:
        reddit = praw.Reddit(
            client_id=settings.config["reddit"]["creds"]["client_id"],
            client_secret=settings.config["reddit"]["creds"]["client_secret"],
            user_agent="Accessing Reddit threads",
            username=username,
            passkey=passkey,
            check_for_async=False,
        )
    except ResponseException as e:
        if e.response.status_code == 401:
            print("Invalid credentials - please check them in config.toml")
    except:
        print("Something went wrong...")

    # Ask user for subreddit input
    print_step("Getting subreddit threads...")
    similarity_score = 0
    if not settings.config["reddit"]["thread"]["subreddit"]:
        try:
            subreddit = reddit.subreddit(
                re.sub(r"r\/", "", input("What subreddit would you like to pull from? "))
            )
        except ValueError:
            subreddit = reddit.subreddit("askreddit")
            print_substep("Subreddit not defined. Using AskReddit.")
    else:
        sub = settings.config["reddit"]["thread"]["subreddit"]
        print_substep(f"Using subreddit: r/{sub} from TOML config")
        subreddit_choice = sub
        if str(subreddit_choice).casefold().startswith("r/"):
            subreddit_choice = subreddit_choice[2:]
        subreddit = reddit.subreddit(subreddit_choice)

    if POST_ID:
        submission = reddit.submission(id=POST_ID)
    elif (
        settings.config["reddit"]["thread"]["post_id"]
        and len(str(settings.config["reddit"]["thread"]["post_id"]).split("+")) == 1
    ):
        submission = reddit.submission(id=settings.config["reddit"]["thread"]["post_id"])
    elif settings.config["ai"]["ai_similarity_enabled"]:
        threads = subreddit.hot(limit=50)
        keywords = settings.config["ai"]["ai_similarity_keywords"].split(",")
        keywords = [keyword.strip() for keyword in keywords]
        keywords_print = ", ".join(keywords)
        print(f"Sorting threads by similarity to the given keywords: {keywords_print}")
        threads, similarity_scores = sort_by_similarity(threads, keywords)
        submission, similarity_score = get_subreddit_undone(
            threads, subreddit, similarity_scores=similarity_scores
        )
    else:
        threads = subreddit.hot(limit=25)
        submission = get_subreddit_undone(threads, subreddit)

    if submission is None:
        return get_subreddit_threads(POST_ID)

    elif not submission.num_comments and settings.config["settings"]["storymode"] == "false":
        print_substep("No comments found. Skipping.")
        exit()

    submission = check_done(submission)

    upvotes = submission.score
    ratio = submission.upvote_ratio * 100
    num_comments = submission.num_comments
    threadurl = f"https://new.reddit.com/{submission.permalink}"

    print_substep(f"Video will be: {submission.title} :thumbsup:", style="bold green")
    print_substep(f"Thread url is: {threadurl} :thumbsup:", style="bold green")
    print_substep(f"Thread has {upvotes} upvotes", style="bold blue")
    print_substep(f"Thread has a upvote ratio of {ratio}%", style="bold blue")
    print_substep(f"Thread has {num_comments} comments", style="bold blue")
    if similarity_score:
        print_substep(
            f"Thread has a similarity score up to {round(similarity_score * 100)}%",
            style="bold blue",
        )

    content["thread_url"] = threadurl
    content["thread_title"] = submission.title
    content["thread_id"] = submission.id
    content["is_nsfw"] = submission.over_18
    content["comments"] = []
    if settings.config["settings"]["storymode"]:
        if settings.config["settings"]["storymodemethod"] == 1:
            content["thread_post"] = posttextparser(submission.selftext)
        else:
            content["thread_post"] = submission.selftext
    else:
        for top_level_comment in submission.comments:
            if isinstance(top_level_comment, MoreComments):
                continue

            if top_level_comment.body in ["[removed]", "[deleted]"]:
                continue
            if _contains_blocked_words(top_level_comment.body):
                continue
            if not top_level_comment.stickied:
                sanitised = sanitize_text(top_level_comment.body)
                if not sanitised or sanitised == " ":
                    continue
                if len(top_level_comment.body) <= int(
                    settings.config["reddit"]["thread"]["max_comment_length"]
                ):
                    if len(top_level_comment.body) >= int(
                        settings.config["reddit"]["thread"]["min_comment_length"]
                    ):
                        if (
                            top_level_comment.author is not None
                            and sanitize_text(top_level_comment.body) is not None
                        ):
                            content["comments"].append(
                                {
                                    "comment_body": top_level_comment.body,
                                    "comment_url": top_level_comment.permalink,
                                    "comment_id": top_level_comment.id,
                                }
                            )

    print_substep("Received subreddit threads Successfully.", style="bold green")
    return content
