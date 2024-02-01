# ruff: noqa: W291
"""Procedures for getting a cookies file."""

_GET_COOKIES_PROCEDURE = """
**IMPORTANT**: During this procedure, do _**NOT**_ sign out of Google.

1. Install "Get cookies.txt LOCALLY" by going to:

   [Get cookies.txt LOCALLY]({store})

2. Click the Extensions icon (puzzle piece) near the top-right of the browser window, \
   then to the right of "Get cookies.txt LOCALLY" click the {settings} and select \
   "Manage extension". {allow}
3. Open Google Maps in a new **{window}** window by going to:

   https://www.google.com/maps

4. Click on the "Sign in" box in the top-right part of the page and follow the \
   prompts. If a "Don't ask again on this device" box appears, make sure it is checked.
5. {extra}Click the Extensions icon and select "Get cookes.txt LOCALLY". \
   A window will appear with all the cookies for the page.
6. Make sure "Export Format" is set to Netscape, then click Export (or Export As.)
7. Immediately close the {window} window.
"""

_CHROME_STORE = (
    "https://chrome.google.com/webstore/detail/get-cookiestxt-locally/"
    "cclelndahbckbenkjhflpdbgdldlbecc"
)

_FIREFOX_ALLOW = """\
For "Run in Private Windows" select "Allow". \
Alternatively, right after the extension is installed, a window should pop up with an \
option to "Allow this extension to run in Private Windows". If so, check that box and \
click Okay.
"""
_FIREFOX_EXTRA = """
Click the Extensions icon, then to the right of "Get cookies.txt LOCALLY" click the \
gear icon and make sure "Always allow on www.google.com" is selected. 
"""
_FIREFOX_STORE = "https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/"

CHROME_PROCEDURE = _GET_COOKIES_PROCEDURE.format(
    allow='Turn on "Allow in Incognito".',
    extra="",
    settings="three-dot (More options) menu",
    store=_CHROME_STORE,
    window="Incognito",
)
# Microsoft Edge can use Chrome extensions.
EDGE_PROCEDURE = _GET_COOKIES_PROCEDURE.format(
    allow='Check "Allow in InPrivate".',
    extra="",
    settings="three-dot (More actions) menu",
    store=_CHROME_STORE,
    window="InPrivate",
)
FIREFOX_PROCEDURE = _GET_COOKIES_PROCEDURE.format(
    allow=_FIREFOX_ALLOW,
    extra=_FIREFOX_EXTRA,
    settings="gear icon",
    store=_FIREFOX_STORE,
    window="Private",
)
