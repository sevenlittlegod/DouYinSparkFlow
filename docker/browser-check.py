"""Launch the configured browser without signing in or sending messages."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import load_configuration


def main():
    load_configuration()
    from core.browser import get_browser
    playwright, browser = get_browser()
    try:
        page = browser.new_page()
        page.goto('about:blank')
        if page.evaluate('1 + 1') != 2:
            raise RuntimeError('Browser check failed')
        print('Browser launched and evaluated a blank page. No login or message was sent.', flush=True)
    finally:
        browser.close()
        playwright.stop()


if __name__ == '__main__':
    main()
