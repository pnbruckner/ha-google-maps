# <img src="https://brands.home-assistant.io/google_maps/icon.png" alt="Google Maps" width="50" height="50"/> Google Maps

This is a custom version of the Home Assistant built-in [Google Maps](https://www.home-assistant.io/integrations/google_maps/) integration.

The main new features are:

<details>
<summary>UI-Based Configuration</summary>

The integration can now be set up via the UI.
YAML-based configuration is still supported but deprecated.

Entities created via the UI do not conflict with any existing "legacy" entities previously created by the built-in integration
(i.e., those that use [`known_devices.yaml`](https://www.home-assistant.io/integrations/device_tracker#known_devicesyaml).)

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
- Automatically initiating reconfiguration when they expire, either at startup or while Home Assistant is running.

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

Location data (`latitude`, etc.) will be used, even if it is "inaccurate" (i.e., does not meet the [set limit](#gps-accuracy-limit)
when there is no previous location data (e.g., the first time the entity is created),
or if the previous location is also inaccurate, as long as the new data is at least as accurate as the old data.
Once "accurate" location data is used, however, it will only be replaced by newer, accurate location data.

</details>

## Installation

<details>
<summary>With HACS</summary>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

You can use HACS to manage the installation and provide update notifications.

1. Add this repo as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/):

```text
https://github.com/pnbruckner/ha-google-maps
```

2. Install the integration using the appropriate button on the HACS Integrations page. Search for "google maps".

</details>

<details>
<summary>Manual</summary>

Place a copy of the files from [`custom_components/google_maps`](custom_components/google_maps)
in `<config>/custom_components/google_maps`,
where `<config>` is your Home Assistant configuration directory.

>__NOTE__: When downloading, make sure to use the `Raw` button from each file's page.

</details>

## Configuration Options

The following options are presented during initial setup and (except for username) when reconfiguring the integration entry (i.e., via the `CONFIGURE` button.)

### Google Maps Account Username

The email address for the Google account to be used for retrieving shared location.

### Cookies File

A cookies file must be [obtained](#obtaining-a-cookies-file) that authenticates and authorizes the user to access the specified Google account.
If an existing cookies file exists from the legacy implementation, and it is still valid, an option will be presented to use that file.
If not, a new cookies file can be uploaded via file browsing or drag & drop.
There is no requirement for the file's name as there was in the legacy implementation.

### Account Tracker Entity

The Google account specified above is used to create `device_tracker` entities for everyone who has shared their location with that account.
In addition to those shared accounts, the integration can also create a tracker for the account itself if it has been associated with a device (phone, etc.)
Unfortunately, though, that tracker will be missing some data (battery level, etc.)
Since there may not be a device associated with the account, or even if there is, the tracker will be missing some data,
an option is provided to enable or disable the creation of this "account tracker entity."
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

This option control the time between requests for updates.

## Account Strategies

It is possible to "register" more than one Google account.
This section attempts to explain why & when you might want to add multiple accounts.

The main things to consider are:
1. The "account entity", if available and used, will be missing some data (as explained in [Account Tracker Entity](#account-tracker-entity).)
2. Which account, or accounts, others need to share their location with.

For strategies below it is assumed that everyone you care about tracking has already shared their location with your personal Google Account,
or is willing to do so.

Strategy | Use Acct Entity | Accts shared w/ HA only Acct | Advantages | Disadvantages
-|-|-|-|-
Personal acct only | Yes | N/A | No additional accts to create & manage. | Your personal tracker will be missing some data.
HA only acct | No | Everybody | Your personal tracker will _not_ be missing data. Everyone can decide if they want to share their location w/ HA. | Create & manage additional Google Acct. Everyone has to share their location with another Google acct.
Personal acct + HA only acct | No | Only your personal acct | Your personal tracker will _not_ be missing data. | Create & manage additional Google Acct. Others cannot independently control data being shared with HA.

The last strategy is probably the best overall, but one of the other two may better suit your personal situation or needs.

## Obtaining a Cookies File

## Removing Legacy Trackers

If/when any "legacy" trackers are no longer desired, they can be removed from the system by:

1. Removing associated YAML configuration entry or entries.
2. Removing associated entries in `known_devices.yaml`.
If that would make the file empty, then the file can simply be deleted instead.
3. Restarting Home Assistant.
