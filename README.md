# <img src="https://brands.home-assistant.io/google_maps/icon.png" alt="Google Maps" width="50" height="50"/> Google Maps

This is a custom version of the Home Assistant built-in [Google Maps](https://www.home-assistant.io/integrations/google_maps/) integration.

The main new features are:

<details>
<summary>Entity-Based Instead of Legacy</summary>

The built-in integration is still what is referred to as a "legacy" tracker.
The entities it creates are customized via [`known_devices.yaml`](https://www.home-assistant.io/integrations/device_tracker#known_devicesyaml).

This custom integration is now "Entity-based".
The entities it creates are managed in the Entity Registry, just like with most newer integrations.
This allows the user to change the entity's name, ID, associated area, etc., as well as enable/disable the entity.

</details>

<details>
<summary>UI-Based Configuration</summary>

The integration can now be set up via the UI.
YAML-based configuration is still supported but deprecated.

Entities created via the UI do not conflict with any existing "legacy" entities previously created by the built-in integration.
The legacy entity IDs use serial numbers or email addresses, whereas the newer IDs use full names.
Therefore, it is possible to continue using the legacy entities even after new ones are created via the UI.
Once you are satisfied the new ones work, and have adjusted your system to use the new IDs,
you can [remove](#removing-legacy-trackers) the legacy entities.

</details>

<details>
<summary>Simplified Cookie Management</summary>

Regarding cookies, unfortunately a cookies file must still be obtained externally,
but managing those cookies is made a bit easier, including:
- Automatically finding and using an existing cookies file used by the legacy implementation.
- Detailed instructions for obtaining a new cookies file.
- Providing a simplified process to upload a new cookies file.
- Displaying the expiration date [^1].
- The creation of a repair issue when they will expire in the near future.
- Automatically initiating reconfiguration when they do expire.

[^1]: Expiration date is determined by looking for cookies named `__Secure-1PSID` or `__Secure-3PSID`.
It is not entirely clear these are the only cookies that impact when the set of cookies will expire.

</details>

<details>
<summary>Enhanced Error Checking and Handling</summary>

Previously, cookies were only "validated" at startup.
If they expired while Home Assistant was running the associated tracker entities would simply stop updating.
Also networking (and other errors) would only be logged, possibly due to an unhandled exception, but no action was taken.

This new implementation adds significantly more error checking.
E.g., cookie validity is checked after every update
and networking errors are caught and handled.

</details>

<details>
<summary>Saving and Restoring State</summary>

Tracker state is saved & restored:
- accross Home Assistant restarts
- through integration entry reload
- when cookies are updated, e.g., during the reauthentication process after existing cookies expire

</details>

<details>
<summary>Refined Filtering of Data Updates</summary>

The built-in integration completely ignores new shared data if the `gps_accuracy` value exceeds the set limit.
This can cause an `unknown` or `unavailable` state when no data was previously received (since startup) that did meet the limit.

The new implementation will always use non-location data (battery level, etc.) regardless of the `last_seen` timestamp.

Location data (`latitude`, etc.) will be used, even if it is "inaccurate" (i.e., does not meet the [set limit](#gps-accuracy-limit)),
when there is no previous location data (e.g., the first time the entity is created),
or if the previous location is also inaccurate, as long as the new data is at least as accurate as the old data.
Once "accurate" location data is used, however, it will only be replaced by newer, accurate location data.

</details>

## Installation

<details>
<summary>With HACS</summary>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

You can use HACS to manage the installation and provide update notifications.

1. Add this repo as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/).
   It should then appear as a new integration. Click on it. If necessary, search for "google maps".

   ```text
   https://github.com/pnbruckner/ha-google-maps
   ```
   Or use this button:
   
   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=pnbruckner&repository=ha-google-maps&category=integration)


1. Download the integration using the appropriate button.

</details>

<details>
<summary>Manual</summary>

Place a copy of the files from [`custom_components/google_maps`](custom_components/google_maps)
in `<config>/custom_components/google_maps`,
where `<config>` is your Home Assistant configuration directory.

>__NOTE__: When downloading, make sure to use the `Raw` button from each file's page.

</details>

After it has been downloaded you will need to restart Home Assistant.

### Versions

This custom integration supports HomeAssistant versions 2023.7.0 or newer.

## Configuration

One or more Google account can be added. See [Account Strategies](#account-strategies) below for help in deciding which Google accounts to use, and possibly create, and how to set up location sharing between Google accounts. If you choose a strategy that uses more than one Google account, obtain the cookies file for each account and upload them individually by adding the integration once for each file. You can also use the **`ADD ENTRY`** button within the Google Maps integration page to add the additional Google accounts after the first one has been created.

To add an account, you can use this My Button:

[![add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=google_maps)

Alternatively, go to Settings -> Devices & services and click the **`+ ADD INTEGRATION`** button.
Find or search for "Google Maps", click on it, then follow the prompts.

> NOTE:
> If you see "Google" in the list, do NOT click on it.
> It will take you to a sub-list of all Google related integrations.
> There will also be a "Google Maps" item in that list, but it is for the built-in integration, not this custom one.

<details>
<summary>Configuration Options</summary>

The following options are presented when adding a Google account, and (except for username) when reconfiguring it (i.e., via the `CONFIGURE` button.)

### Google Maps Account Username

The email address for the Google account to be used for retrieving shared location.

### Cookies File

A cookies file must be [obtained](#obtaining-a-cookies-file) that authenticates and authorizes the user to access the specified Google account.
If a cookies file exists from the legacy implementation, and it is still valid, an option will be presented to use that file.
If not, a new cookies file can be uploaded via file browsing or drag & drop.
There is no requirement for the file's name as there was in the legacy implementation.

### Account Tracker Entity

The Google account specified above is used to create `device_tracker` entities for everyone who has shared their location with that account.
In addition to those shared accounts, the integration can also create a tracker for the account itself if it has been associated with a device (phone, etc.)
Unfortunately, though, that tracker will be [missing some data](#missing-data-for-account-tracker).
Since there may not be a device associated with the account, or even if there is, the tracker will be missing some data,
an option is provided to enable or disable the creation of this "account tracker" entity.
See [Account Strategies](account-strategies) for more detail.

### GPS Accuracy Limit

Each location update has an accuracy value that, together with the latitude & longitude, describes a circular area in which the device may actually be.
Under certain conditions (poor GPS, cell or network coverage, etc.) that accuracy value can become quite large.
(The _larger_ the accuracy value, the _less_ accurate the location fix.)

To avoid undesired effects (such as the device appearing to be in multiple Home Assitant Zones, or simply appearing to be jumping around)
an upper limit is used.
If the reported accuracy is _less than or equal to_ the specified limit, then the location data is considered "accurate."
If, however, the reported accuracy is _more than_ the specified limit, then the location data is considered "inaccurate."

See the description above about "Refined Filtering of Data Updates" for more information about how this limit is used.

### Update Period

This option controls the time between requests for updates.

</details>

## Missing Data for Account Tracker

The ["account holder" tracker entity](#account-tracker-entity), if created,
will be missing some data that is usually present for the tracker entities created for those that have shared their location with that account.
Specifically, the name (and entity ID) will be based on the email address, not the actual name associated with the account,
and the following attributes will be missing or invalid:

`battery_charging`, `battery_level`, `entity_picture`, `full_name` & `nickname`

All other attributes, including those related to location, will be present and valid.

## Account Strategies

As mentioned in [Configuration](#configuration), it is possible to add more than one Google account for this integration.
This section explains why & when you might want to add multiple accounts.

The main things to consider are:
1. The "account tracker", if available and used, will be [missing some data](#missing-data-for-account-tracker).
2. Which account (or accounts) others need to share their location with.

The strategies described below refer to two different Google accounts:

"Pers acct" refers to your own, personal Google account that is associated with a "device"
(phone, tablet, etc.) from which your location can be obtained.

"Alt acct" refers to an alternate Google account, either one that already exists,
or one you create just for use with Home Assistant.

For the sake of simplicity, it is assumed that everyone you care to track in Home Assistant
has already shared their location with your personal Google account,
or is probably willing to do do.
However, that may not be the case, so adjust accordingly.

Strategy | Use Acct Tracker | Acct Sharing | Advantages | Disadvantages
-|-|-|-|-
Pers acct only | Yes | Others share w/ Pers acct. | No additional accts to create & manage. No additional location sharing to set up. | Your personal tracker will be missing some data. Nobody can independently control data being shared with HA.
Alt acct only | No | Everybody who wants to, including yourself, shares w/ Alt acct. | Your personal tracker will _not_ be missing data. Everyone can decide if they want to share their location w/ HA, including yourself. | Possibly create & manage an additional acct. People may need to share their location w/ a 2nd acct.
Pers & Alt accts | No | You share w/ Alt acct, everyone else shares w/ Pers acct. | Your personal tracker will _not_ be missing data. Nobody else has to change their location sharing. | Create & manage additional Google Acct. Others cannot independently control data being shared with HA.

The last strategy is probably the best overall, and if chosen will require obtaining the cookies files for both accounts and adding them both to the integration separately. One of the other two strategies may better suit your personal situation or needs.

## Obtaining a Cookies File

> **IMPORTANT**:
> 
> In the procedures that follow you will sign into Google.
> It is extremely important to NOT log out from Google in the browser you use to get the cookies file.
> Just close the browser as the procedures instruction _without_ logging out first.

The procedure for each browser type uses the [Get cookies.txt LOCALLY](https://github.com/kairi003/Get-cookies.txt-Locally) extension.
There is one version of the extension that works in Chrome and Edge, and another for Firefox.

<details>
<summary>Google Chrome</summary>

1. Install "Get cookies.txt LOCALLY", which can be found by browsing to:

   https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc

2. Click the Extensions icon (puzzle piece) near the top-right of the browser window,
   then to the right of "Get cookies.txt LOCALLY" click the three-dot (More options) item
   and select "Manage extension". Turn on "Allow in Incognito".
3. Open a new Incognito window and browse to:

   google.com/maps

4. Click on the "Sign in" box in the top-right part of the page and follow the prompts.
   If a "Don't ask again on this device" box appears, make sure it is checked.
5. Click the Extensions icon and select "Get cookes.txt LOCALLY".
   A window will appear with all the cookies for the page.
6. Make sure "Export Format" is set to Netscape, then click Export (or Export As.)
7. Immediately close the Incognito window.

</details>

<details>
<summary>Microsoft Edge</summary>

1. Install "Get cookies.txt LOCALLY", which can be found by browsing to:

   https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc

2. Click the Extensions icon (puzzle piece) near the top-right of the browser window,
   then to the right of "Get cookies.txt LOCALLY" click the three-dot (More actions) item
   and select "Manage extension". Turn on "Allow in InPrivate".
3. Open a new InPrivate window and browse to:

   google.com/maps

4. Click on the "Sign in" box in the top-right part of the page and follow the prompts.
   If a "Don't ask again on this device" box appears, make sure it is checked.
5. Click the Extensions icon and select "Get cookes.txt LOCALLY".
   A window will appear with all the cookies for the page.
6. Make sure "Export Format" is set to Netscape, then click Export (or Export As.)
7. Immediately close the InPrivate window.

</details>

<details>
<summary>Mozilla Firefox</summary>

1. Install "Get cookies.txt LOCALLY", which can be found by browsing to:

   https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/

2. After the add-on is installed a window should pop up with an option to
   "Allow this extension to run in Private Windows". Check that box and click Okay.
   Alternatively, click the Extensions icon (puzzle piece) near the top-right of the
   browser window, then to the right of "Get cookies.txt LOCALLY" click the gear icon
   and select "Manage Extension". For "Run in Private Windows" select "Allow".
3. Open a new Private window and browse to:

   google.com/maps

4. Click on the "Sign in" box in the top-right part of the page and follow the prompts.
   If a "Don't ask again on this device" box appears, make sure it is checked.
5. Click the Extensions icon, then to the right of "Get cookies.txt LOCALLY" click the gear
   icon and make sure "Always allow on www.google.com" is selected. Click the Extensions icon
   again and select "Get cookes.txt LOCALLY". A window will appear with all the cookies for
   the page.
6. Make sure "Export Format" is set to Netscape, then click Export (or Export As.)
7. Immediately close the Private window.

</details>

## Removing Legacy Trackers

If/when any "legacy" trackers are no longer desired, they can be removed from the system by:

1. Removing associated YAML configuration entry or entries.
2. Removing associated entries in `known_devices.yaml`.
If that would make the file empty, then the file can simply be deleted instead.
3. Restarting Home Assistant.
