import os
import re
import random
from pathlib import Path
from typing import Final

from jinja2 import Template
from playwright.sync_api import sync_playwright
from rich.progress import track

from utils import settings
from utils.console import print_step, print_substep
from utils.imagenarator import imagemaker

__all__ = ["get_screenshots_of_reddit_posts"]

# Directory where HTML templates live
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "screenshot_templates")

# Pool of avatar colors for visual variety
AVATAR_COLORS = [
    "#FF4500", "#0079D3", "#46D160", "#FF6600", "#FFB000",
    "#7B68EE", "#FF585B", "#00A6A5", "#CC3600", "#0DD3BB",
    "#46A508", "#FF66AC", "#9E8D49", "#008985", "#4856A3",
]


def _load_template(filename: str) -> Template:
    """Load a Jinja2 template from the templates directory."""
    filepath = os.path.join(TEMPLATE_DIR, filename)
    with open(filepath, "r", encoding="utf-8") as f:
        return Template(f.read())


def _get_theme_vars(theme: str) -> dict:
    """Return CSS color variables for dark or light theme."""
    if theme == "dark":
        return {
            "bg_color": "transparent",
            "card_bg": "#1A1A1B",
            "text_color": "#D7DADC",
            "meta_color": "#818384",
            "border_color": "#343536",
            "accent_color": "#FF4500",
            "link_color": "#4FBCFF",
            "code_bg": "#272729",
        }
    else:  # light
        return {
            "bg_color": "transparent",
            "card_bg": "#FFFFFF",
            "text_color": "#1C1C1C",
            "meta_color": "#7C7C7C",
            "border_color": "#EDEFF1",
            "accent_color": "#FF4500",
            "link_color": "#0079D3",
            "code_bg": "#F6F7F8",
        }


def _format_score(score: int) -> str:
    """Format large numbers: 1500 -> 1.5k, 1500000 -> 1.5m"""
    if score >= 1_000_000:
        return f"{score / 1_000_000:.1f}m"
    elif score >= 1_000:
        return f"{score / 1_000:.1f}k"
    return str(score)


def get_screenshots_of_reddit_posts(reddit_object: dict, screenshot_num: int):
    """
    Generate screenshots of reddit posts and comments using HTML templates.
    Renders locally via Playwright — no Reddit login or navigation required.

    Args:
        reddit_object (dict): Reddit object received from reddit/subreddit.py
        screenshot_num (int): Number of comment screenshots to generate
    """
    # Settings
    W: Final[int] = int(settings.config["settings"]["resolution_w"])
    H: Final[int] = int(settings.config["settings"]["resolution_h"])
    theme: Final[str] = settings.config["settings"]["theme"]
    storymode: Final[bool] = settings.config["settings"]["storymode"]

    print_step("Generating screenshots from templates...")
    reddit_id = re.sub(r"[^\w\s-]", "", reddit_object["thread_id"])

    # Ensure output directory exists
    Path(f"assets/temp/{reddit_id}/png").mkdir(parents=True, exist_ok=True)

    # Get theme colors
    theme_vars = _get_theme_vars(theme if theme != "transparent" else "dark")

    # Screenshot width: 45% of video width (matching original bot behavior)
    screenshot_width = int((W * 45) // 100)

    # Handle transparent story mode with imagemaker (same as original)
    if storymode and settings.config["settings"]["storymodemethod"] == 1 and theme == "transparent":
        bgcolor = (0, 0, 0, 0)
        txtcolor = (255, 255, 255)
        print_substep("Generating transparent story images...")
        return imagemaker(
            theme=bgcolor,
            reddit_obj=reddit_object,
            txtclr=txtcolor,
            transparent=True,
        )

    # Load templates
    post_template = _load_template("post.html")
    comment_template = _load_template("comment.html")
    story_template = _load_template("story.html")

    # Launch browser ONCE, reuse for all renders
    with sync_playwright() as p:
        print_substep("Launching renderer...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": screenshot_width + 32, "height": 1},
            device_scale_factor=2,  # 2x for crisp text on high-DPI video
        )
        page = context.new_page()

        # ---- Render post title screenshot ----
        subreddit_name = reddit_object.get("subreddit_name", "reddit")
        post_html = post_template.render(
            **theme_vars,
            width=screenshot_width,
            subreddit_name=subreddit_name,
            subreddit_initial=subreddit_name[0].upper() if subreddit_name else "R",
            author=reddit_object.get("thread_author", "[deleted]"),
            title=reddit_object["thread_title"],
            score=_format_score(reddit_object.get("thread_score", 0)),
            num_comments=_format_score(reddit_object.get("thread_num_comments", 0)),
            is_nsfw=reddit_object.get("is_nsfw", False),
        )

        page.set_content(post_html)
        page.wait_for_load_state("networkidle")
        page.locator(".screenshot-target").screenshot(
            path=f"assets/temp/{reddit_id}/png/title.png"
        )
        print_substep("Title screenshot generated.")

        # ---- Story mode rendering ----
        if storymode:
            if settings.config["settings"]["storymodemethod"] == 0:
                # Single story content image
                selftext_html = reddit_object.get("selftext_html", "")
                if not selftext_html:
                    # Fallback: wrap plain text in paragraphs
                    selftext_html = "<p>" + reddit_object.get("thread_post", "").replace("\n", "</p><p>") + "</p>"

                story_html = story_template.render(
                    **theme_vars,
                    width=screenshot_width,
                    body_html=selftext_html,
                )
                page.set_content(story_html)
                page.wait_for_load_state("networkidle")
                page.locator(".screenshot-target").screenshot(
                    path=f"assets/temp/{reddit_id}/png/story_content.png"
                )
                print_substep("Story content screenshot generated.")

            elif settings.config["settings"]["storymodemethod"] == 1:
                # Multiple story segment images
                texts = reddit_object.get("thread_post", [])
                if isinstance(texts, str):
                    texts = [texts]

                for idx, text in track(enumerate(texts), "Rendering story images..."):
                    segment_html = "<p>" + text.replace("\n", "</p><p>") + "</p>"
                    story_html = story_template.render(
                        **theme_vars,
                        width=screenshot_width,
                        body_html=segment_html,
                    )
                    page.set_content(story_html)
                    page.wait_for_load_state("networkidle")
                    page.locator(".screenshot-target").screenshot(
                        path=f"assets/temp/{reddit_id}/png/img{idx}.png"
                    )

                print_substep("Story segment screenshots generated.")

        else:
            # ---- Render comment screenshots ----
            for idx, comment in enumerate(
                track(
                    reddit_object["comments"][:screenshot_num],
                    "Rendering comment screenshots...",
                )
            ):
                if idx >= screenshot_num:
                    break

                author = comment.get("comment_author", "[deleted]")
                avatar_color = AVATAR_COLORS[hash(author) % len(AVATAR_COLORS)]
                score = comment.get("comment_score", 0)

                # Use body_html if available, fall back to plain text wrapped in <p>
                body_html = comment.get("comment_body_html", "")
                if not body_html:
                    body_html = "<p>" + comment["comment_body"].replace("\n", "</p><p>") + "</p>"

                comment_html = comment_template.render(
                    **theme_vars,
                    width=screenshot_width,
                    author=author,
                    author_initial=author[0].upper() if author and author != "[deleted]" else "?",
                    avatar_color=avatar_color,
                    score=_format_score(score),
                    body_html=body_html,
                )

                page.set_content(comment_html)
                page.wait_for_load_state("networkidle")
                page.locator(".screenshot-target").screenshot(
                    path=f"assets/temp/{reddit_id}/png/comment_{idx}.png"
                )

            print_substep("Comment screenshots generated.")

        # Clean up — single browser close
        browser.close()

    print_substep("Screenshots generated successfully.", style="bold green")
