# <img src="https://brands.home-assistant.io/google_maps/icon.png" alt="Google Maps" width="50" height="50"/> Google Maps

This is a custom version of the Home Assistant built-in [Google Maps](https://www.home-assistant.io/integrations/google_maps/) integration.

It extends the built-in integration in ways that make sense, but are no longer accepted practice.

## Installation
### With HACS
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

You can use HACS to manage the installation and provide update notifications.

1. Add this repo as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/):

```text
https://github.com/pnbruckner/ha-google-maps
```

2. Install the integration using the appropriate button on the HACS Integrations page. Search for "google maps".

### Manual

Place a copy of the files from [`custom_components/google_maps`](custom_components/google_maps)
in `<config>/custom_components/google_maps`,
where `<config>` is your Home Assistant configuration directory.

>__NOTE__: When downloading, make sure to use the `Raw` button from each file's page.

## Setup

Please see the standard [Google Maps](https://www.home-assistant.io/integrations/google_maps/) documentation for basic set up.

Note that YAML configuration is still supported, but is deprecated.
Use the Integrations UI instead.

After creating one or more cookie files (for one or more account), when configuring via the UI,
don't bother renaming the file or copying it onto your Home Assistant system.
The UI config flow will provide an option to upload the file.
Also, if you already have an existing cookies file (renamed and copied onto the system per the original documentation),
then there is no need to create a new cookies file.
The UI config flow will automatically detect the presence of the cookie file and give you the option to "import" it.
After it is imported, and after you remove google_maps from your YAML configuration,
and delete any related entries in known_devices.yaml,
you can delete the cookies file in your configuration directory.
