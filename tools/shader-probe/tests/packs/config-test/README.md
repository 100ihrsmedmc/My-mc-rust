# config-test

A minimal Horizon shader pack that exercises the loader's own configuration readers
(`horizon-shaders::options` and `Screens::parse`, spec 8.2 and 8.3). It declares
switch and value options, groups them into two screens, marks two as sliders, and
defines two profiles. The headless probe reads all of it with no GPU, which is how the
workflow proves the "configuration and pack metadata are preserved" finding.

Written for Horizon; no other pack's code is used.
