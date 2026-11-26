# Changelog

## [0.32.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.32.0...v0.32.1) (2026-11-26)


### Bug Fixes

* prevent ATW zero-energy resets on transient 0x10 errors and add total thermal energy ([#211](https://github.com/tgenov/ha-iletcomfort/issues/211)) ([cfbfe2a](https://github.com/tgenov/ha-iletcomfort/commit/cfbfe2ab8961c6312187a68c1beae4f878631f32))

## [0.32.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.31.0...v0.32.0) (2026-11-26)


### Features

* add dual-zone ATW (171H120F) Zone 2 climate and curve support ([#208](https://github.com/tgenov/ha-iletcomfort/issues/208)) ([3462494](https://github.com/tgenov/ha-iletcomfort/commit/34624945b4478859572046b9663d5c25246b467f))

## [0.31.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.30.0...v0.31.0) (2026-11-26)


### Features

* extend opt-in ATW (171H120F) C3 basic control ([#206](https://github.com/tgenov/ha-iletcomfort/issues/206)) ([3a6221e](https://github.com/tgenov/ha-iletcomfort/commit/3a6221e051cac51d3646960967ed0b1b0292b19a))

## [0.30.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.29.0...v0.30.0) (2026-11-26)


### Features

* **atw:** unify 171H120F C3BasicStatus read decoding across all masks ([#204](https://github.com/tgenov/ha-iletcomfort/issues/204)) ([56ad33f](https://github.com/tgenov/ha-iletcomfort/commit/56ad33f716fefb5d5096de2990d65be07f40a6f4))

## [0.29.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.28.0...v0.29.0) (2026-11-26)


### Features

* **atw:** probe and decode 0x05 Silent/Super-Silent and 0x07 ECO ([#201](https://github.com/tgenov/ha-iletcomfort/issues/201)) ([49665c4](https://github.com/tgenov/ha-iletcomfort/commit/49665c4150b9b584a196b39cc87ffdd28b9e5296))

## [0.28.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.27.1...v0.28.0) (2026-11-26)


### Features

* opt-in control of 171H120F units via standard Midea C3 SET ([#42](https://github.com/tgenov/ha-iletcomfort/issues/42)) ([#143](https://github.com/tgenov/ha-iletcomfort/issues/143)) ([3e6fc2b](https://github.com/tgenov/ha-iletcomfort/commit/3e6fc2b87d8be423b68f86423aefdcbbf69706f7))

## [0.27.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.27.0...v0.27.1) (2026-11-26)


### Bug Fixes

* **coordinator,atw:** fix MQTT watchdog callback thread-safety and Cool-tag schedule placeholders ([#199](https://github.com/tgenov/ha-iletcomfort/issues/199)) ([c370641](https://github.com/tgenov/ha-iletcomfort/commit/c370641160fa5bdeaf0e110137c2703cb18fd820))

## [0.27.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.26.0...v0.27.0) (2026-11-26)


### Features

* **kjrh120l:** add DHW power switch for dual Zone-1 units ([#196](https://github.com/tgenov/ha-iletcomfort/issues/196)) ([debf4e1](https://github.com/tgenov/ha-iletcomfort/commit/debf4e1f183c4dbc32206a10a499e87ac472f0ad))

## [0.26.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.25.0...v0.26.0) (2026-11-26)


### Features

* add integration brand icon and enable full HACS validation ([#185](https://github.com/tgenov/ha-iletcomfort/issues/185)) ([f8f327f](https://github.com/tgenov/ha-iletcomfort/commit/f8f327f3439ebcd62e80164b857dfe9da8400f93))

## [0.25.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.24.0...v0.25.0) (2026-11-26)


### Features

* **atw:** expose verified 0x10 sensors (water flow, DC bus, EXV) ([#178](https://github.com/tgenov/ha-iletcomfort/issues/178)) ([f0588d1](https://github.com/tgenov/ha-iletcomfort/commit/f0588d11fccf8388e9a47736f163db547480a6dd))


### Bug Fixes

* address EPIC [#173](https://github.com/tgenov/ha-iletcomfort/issues/173) code review findings and require clean-context self-review ([#184](https://github.com/tgenov/ha-iletcomfort/issues/184)) ([108e17b](https://github.com/tgenov/ha-iletcomfort/commit/108e17b58cdc853111666f3515e4070f88ae7ac8))

## [0.24.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.23.0...v0.24.0) (2026-11-26)


### Features

* **diagnostics:** preserve raw frames per subtype and expose protocol probe state ([#174](https://github.com/tgenov/ha-iletcomfort/issues/174)) ([eeb767f](https://github.com/tgenov/ha-iletcomfort/commit/eeb767f3d39a7cb4c70ef720ca1031fe5aa94c8e))

## [0.23.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.22.0...v0.23.0) (2026-11-26)


### Features

* **atw:** poll subtype 0x0A for compressor run hours ([#164](https://github.com/tgenov/ha-iletcomfort/issues/164)) ([7a4b257](https://github.com/tgenov/ha-iletcomfort/commit/7a4b257f519a1a4295b2bf56e26f747ef62b755f))

## [0.22.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.21.0...v0.22.0) (2026-11-25)


### Features

* decode the C3 basic layout of 171H120F units ([#124](https://github.com/tgenov/ha-iletcomfort/issues/124)) ([#146](https://github.com/tgenov/ha-iletcomfort/issues/146)) ([cd3f759](https://github.com/tgenov/ha-iletcomfort/commit/cd3f759cbc8fcc70d5acf70f0c7661c37167066e))

## [0.21.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.20.1...v0.21.0) (2026-11-25)


### Features

* **atw:** poll subtype 0x10 live telemetry when supported ([#160](https://github.com/tgenov/ha-iletcomfort/issues/160)) ([980eb6a](https://github.com/tgenov/ha-iletcomfort/commit/980eb6aecb4950bf79612f4a9c8632124c0994fc))

## [0.20.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.20.0...v0.20.1) (2026-11-25)


### Bug Fixes

* **kjrh120l:** keep DHW setpoint and climate consistent when Zone 1 is off ([#158](https://github.com/tgenov/ha-iletcomfort/issues/158)) ([7e8924a](https://github.com/tgenov/ha-iletcomfort/commit/7e8924a4df7e224a165782a4e1596cbf54a81b92))

## [0.20.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.19.1...v0.20.0) (2026-11-25)


### Features

* **kjrh120l:** decode outdoor temperature from status body[57] ([#156](https://github.com/tgenov/ha-iletcomfort/issues/156)) ([f9cf9a3](https://github.com/tgenov/ha-iletcomfort/commit/f9cf9a3d108ebafa06397b8b26229be78dc6b9e2))

## [0.19.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.19.0...v0.19.1) (2026-11-25)


### Bug Fixes

* suppress ATW active schedule-slot sensor frames ([#153](https://github.com/tgenov/ha-iletcomfort/issues/153)) ([cca9a2b](https://github.com/tgenov/ha-iletcomfort/commit/cca9a2bf5ef5a1d6fbe00f6f6991f1762512c185))

## [0.19.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.18.4...v0.19.0) (2026-11-25)


### Features

* **kjrh120l:** control Zone 1 setpoint, run mode and Zone 1 power ([#149](https://github.com/tgenov/ha-iletcomfort/issues/149)) ([52982cd](https://github.com/tgenov/ha-iletcomfort/commit/52982cd19e557669a5ea3e9738b4c0e3643b0dd3))

## [0.18.4](https://github.com/tgenov/ha-iletcomfort/compare/v0.18.3...v0.18.4) (2026-11-17)


### Bug Fixes

* decode NØRDIS 27-byte ATW status and template sensor frames ([#137](https://github.com/tgenov/ha-iletcomfort/issues/137)) ([64ab779](https://github.com/tgenov/ha-iletcomfort/commit/64ab77983f2b92664732d2c9b611deb60eeceddf))

## [0.18.3](https://github.com/tgenov/ha-iletcomfort/compare/v0.18.2...v0.18.3) (2026-09-21)


### Bug Fixes

* decode Galmet ATW power state ([#116](https://github.com/tgenov/ha-iletcomfort/issues/116)) ([d761410](https://github.com/tgenov/ha-iletcomfort/commit/d761410f770ee46a63b767e9698f2f80b57eae6f))

## [0.18.2](https://github.com/tgenov/ha-iletcomfort/compare/v0.18.1...v0.18.2) (2026-09-21)


### Bug Fixes

* decode Galmet ATW status variant ([#114](https://github.com/tgenov/ha-iletcomfort/issues/114)) ([db4acc4](https://github.com/tgenov/ha-iletcomfort/commit/db4acc4ce82113b70a61b36e644a9aef150d59d7))

## [0.18.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.18.0...v0.18.1) (2026-09-18)


### Bug Fixes

* suppress ATW placeholder sensor values ([#102](https://github.com/tgenov/ha-iletcomfort/issues/102)) ([97a0335](https://github.com/tgenov/ha-iletcomfort/commit/97a0335a3f3c2266a43b1bbff6deb266f263bce4))

## [0.18.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.17.0...v0.18.0) (2026-09-12)


### Features

* use MQTT status pushes as heartbeat ([#98](https://github.com/tgenov/ha-iletcomfort/issues/98)) ([1b4a331](https://github.com/tgenov/ha-iletcomfort/commit/1b4a33158a9d31498e1b757a2d3a16932a4d0a4e))

## [0.17.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.16.0...v0.17.0) (2026-09-11)


### Features

* show device model in Home Assistant ([#95](https://github.com/tgenov/ha-iletcomfort/issues/95)) ([5d5e3ad](https://github.com/tgenov/ha-iletcomfort/commit/5d5e3adb79d1aaa180311de087a2df8af349f6b8))

## [0.16.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.15.0...v0.16.0) (2026-09-11)


### Features

* add safe semantic parser probe ([#91](https://github.com/tgenov/ha-iletcomfort/issues/91)) ([49d4256](https://github.com/tgenov/ha-iletcomfort/commit/49d42568ac888b1f5d0c9488eff7cd44d89afe9e))
* extract Weex semantic API catalogues ([#89](https://github.com/tgenov/ha-iletcomfort/issues/89)) ([1d9b09e](https://github.com/tgenov/ha-iletcomfort/commit/1d9b09ef8c036b052a8165d71e05cdb7375868ed))


### Bug Fixes

* retain semantic operation direction in findings ([#92](https://github.com/tgenov/ha-iletcomfort/issues/92)) ([853e21a](https://github.com/tgenov/ha-iletcomfort/commit/853e21ae7aee1c19203ae07a99a810ac802da2a7))

## [0.15.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.14.2...v0.15.0) (2026-09-11)


### Features

* **mqtt:** capture scoped command frames ([#79](https://github.com/tgenov/ha-iletcomfort/issues/79)) ([2ee3111](https://github.com/tgenov/ha-iletcomfort/commit/2ee3111291e722d298f9f3ca39ee1f054904a270))

## [0.14.2](https://github.com/tgenov/ha-iletcomfort/compare/v0.14.1...v0.14.2) (2026-09-11)


### Bug Fixes

* fall back to AQUAPURA outlet temperature ([#76](https://github.com/tgenov/ha-iletcomfort/issues/76)) ([0fdd83b](https://github.com/tgenov/ha-iletcomfort/commit/0fdd83b2e7a31e09b6c4fef2d452e28e0a559494))

## [0.14.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.14.0...v0.14.1) (2026-09-11)


### Bug Fixes

* hide temperature controls in fan-only mode ([#73](https://github.com/tgenov/ha-iletcomfort/issues/73)) ([d7776d1](https://github.com/tgenov/ha-iletcomfort/commit/d7776d1663a4f1cc88d8a242812956d3b4902bff))

## [0.14.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.13.0...v0.14.0) (2026-09-11)


### Features

* renew MQTT certificates before expiry ([#70](https://github.com/tgenov/ha-iletcomfort/issues/70)) ([31dcc97](https://github.com/tgenov/ha-iletcomfort/commit/31dcc970b5a4c8e1adf848658141053def5cd8d5))

## [0.13.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.12.1...v0.13.0) (2026-09-10)


### Features

* split KJRH-120L dual setpoint readings ([#67](https://github.com/tgenov/ha-iletcomfort/issues/67)) ([d763bed](https://github.com/tgenov/ha-iletcomfort/commit/d763bed08e9769182e56150df3ed3520d720b9b2))

## [0.12.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.12.0...v0.12.1) (2026-09-10)


### Bug Fixes

* add missing bA outdoor-range fault code ([#65](https://github.com/tgenov/ha-iletcomfort/issues/65)) ([60a918c](https://github.com/tgenov/ha-iletcomfort/commit/60a918cd8608aabc2d644462d73ae1981f8e4188))

## [0.12.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.11.0...v0.12.0) (2026-09-10)


### Features

* add explicit phone app coexistence mode ([#63](https://github.com/tgenov/ha-iletcomfort/issues/63)) ([4273e72](https://github.com/tgenov/ha-iletcomfort/commit/4273e72570d6458ba5d74daa5e5d62de2bf2b6e9))

## [0.11.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.10.0...v0.11.0) (2026-09-10)


### Features

* expose C3 fault details on error sensor ([#61](https://github.com/tgenov/ha-iletcomfort/issues/61)) ([d04d091](https://github.com/tgenov/ha-iletcomfort/commit/d04d0918f2bb9221c9ac46081cd2e5affe02249d))

## [0.10.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.9.0...v0.10.0) (2026-09-10)


### Features

* track known KJRH-120L2 model code ([#59](https://github.com/tgenov/ha-iletcomfort/issues/59)) ([95fc73f](https://github.com/tgenov/ha-iletcomfort/commit/95fc73f04ea4dc49db2d83f71c66f7ab18f725ab))

## [0.9.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.8.3...v0.9.0) (2026-09-08)


### Features

* MQTT real-time status push (opt-in) that survives the login war ([#57](https://github.com/tgenov/ha-iletcomfort/issues/57)) ([e286c0f](https://github.com/tgenov/ha-iletcomfort/commit/e286c0ff97b92a5025435b8f0cbfd7e6994dc60a))

## [0.8.3](https://github.com/tgenov/ha-iletcomfort/compare/v0.8.2...v0.8.3) (2026-09-08)


### Bug Fixes

* reject unrecognized control layouts and recover model metadata ([#50](https://github.com/tgenov/ha-iletcomfort/issues/50)) ([30a42f3](https://github.com/tgenov/ha-iletcomfort/commit/30a42f3e85134fc0a68589eaa5ef20541f823997))

## [0.8.2](https://github.com/tgenov/ha-iletcomfort/compare/v0.8.1...v0.8.2) (2026-07-11)


### Bug Fixes

* only WARN on sustained cache-fallback failures, DEBUG for transient cloud/DNS blips ([#44](https://github.com/tgenov/ha-iletcomfort/issues/44)) ([#45](https://github.com/tgenov/ha-iletcomfort/issues/45)) ([ae74752](https://github.com/tgenov/ha-iletcomfort/commit/ae74752b4b93e5e7371979edd4a70d280514b5c2))

## [0.8.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.8.0...v0.8.1) (2026-06-09)


### Bug Fixes

* decode KJRH-120L power state (body[10]) and widen setpoint range to 20-70 ([#35](https://github.com/tgenov/ha-iletcomfort/issues/35)) ([#39](https://github.com/tgenov/ha-iletcomfort/issues/39)) ([b6eeaca](https://github.com/tgenov/ha-iletcomfort/commit/b6eeaca87117ef18e6a57b1320c17e65bda72f31))

## [0.8.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.7.3...v0.8.0) (2026-06-08)


### Features

* KJRH-120L power/setpoint control via short write commands ([#35](https://github.com/tgenov/ha-iletcomfort/issues/35)) ([#36](https://github.com/tgenov/ha-iletcomfort/issues/36)) ([f44da76](https://github.com/tgenov/ha-iletcomfort/commit/f44da76e1b3bfa57b0cc7e9158101a19cccf7731))

## [0.7.3](https://github.com/tgenov/ha-iletcomfort/compare/v0.7.2...v0.7.3) (2026-06-08)


### Bug Fixes

* clean KJRH-120L decode — correct DHW setpoint, suppress garbage status/temps ([#21](https://github.com/tgenov/ha-iletcomfort/issues/21), [#5](https://github.com/tgenov/ha-iletcomfort/issues/5)) ([#33](https://github.com/tgenov/ha-iletcomfort/issues/33)) ([ce714a0](https://github.com/tgenov/ha-iletcomfort/commit/ce714a0889c344b6f45a16ed06f5314e6924b4e1))

## [0.7.2](https://github.com/tgenov/ha-iletcomfort/compare/v0.7.1...v0.7.2) (2026-06-07)


### Bug Fixes

* send short query command for KJRH-120L (sn8 17100003) so it connects ([#21](https://github.com/tgenov/ha-iletcomfort/issues/21), [#5](https://github.com/tgenov/ha-iletcomfort/issues/5)) ([#31](https://github.com/tgenov/ha-iletcomfort/issues/31)) ([bd75839](https://github.com/tgenov/ha-iletcomfort/commit/bd75839e0400f0a51991f8222d307d253681da43))

## [0.7.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.7.0...v0.7.1) (2026-06-07)


### Bug Fixes

* suffix-mask appliance_code in diagnostics, logs, and repair card to avoid leaking device IDs ([#28](https://github.com/tgenov/ha-iletcomfort/issues/28)) ([ff48084](https://github.com/tgenov/ha-iletcomfort/commit/ff480849f63f2df106a9804c2ff59a658d3e9cb3))

## [0.7.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.6.0...v0.7.0) (2026-06-05)


### Features

* sn8 model-decode profiles (ATW status [#22](https://github.com/tgenov/ha-iletcomfort/issues/22), AQUAPURA water temp [#12](https://github.com/tgenov/ha-iletcomfort/issues/12)) ([#25](https://github.com/tgenov/ha-iletcomfort/issues/25)) ([b74a9b8](https://github.com/tgenov/ha-iletcomfort/commit/b74a9b84fd600b8d8d88d0e6febcc5f96d2b8a20))

## [0.6.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.5.1...v0.6.0) (2026-06-04)


### Features

* surface appliance metadata in diagnostics to enable model-specific decoding ([90f6a98](https://github.com/tgenov/ha-iletcomfort/commit/90f6a98700b2252b5fcd6e5262afcdab8f79c4a3))
* surface appliance metadata in diagnostics to enable model-specific decoding ([2cae197](https://github.com/tgenov/ha-iletcomfort/commit/2cae197ed935c5f498e828245853c7629de6b9ed))

## [0.5.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.5.0...v0.5.1) (2026-05-30)


### Bug Fixes

* correct ODU Current scaling and compressor detection for MSC-70D2N8-A ([#11](https://github.com/tgenov/ha-iletcomfort/issues/11)) ([dd9d862](https://github.com/tgenov/ha-iletcomfort/commit/dd9d8629cc747fe31d764dbd3eff3bbd9b01ac84))

## [0.5.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.4.0...v0.5.0) (2026-05-29)


### Features

* group entities under a Device and add ODU Current sensor ([f72727f](https://github.com/tgenov/ha-iletcomfort/commit/f72727faf3780ece70055dd0ddc0d36d5363164a))
* group entities under a Device and add ODU Current sensor ([5afd917](https://github.com/tgenov/ha-iletcomfort/commit/5afd917d18f3b5fd37fa8316fa79f83a41f8090e))

## [0.4.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.3.0...v0.4.0) (2026-05-29)


### Features

* add diagnostics download, issue forms, and troubleshooting guide ([f916f19](https://github.com/tgenov/ha-iletcomfort/commit/f916f1970ffaa106f3a961daa89c0e166d41396c))
* add diagnostics download, issue forms, and troubleshooting guide ([e6df83d](https://github.com/tgenov/ha-iletcomfort/commit/e6df83de94d538564eca90e3dee96fad1e68ab79))

## [0.3.0](https://github.com/tgenov/ha-iletcomfort/compare/v0.2.1...v0.3.0) (2026-05-28)


### Features

* surface device-offline state as a Home Assistant Repair card ([0578321](https://github.com/tgenov/ha-iletcomfort/commit/0578321f19214d934afca5dcaaac679e1d6c0986))
* surface device-offline state as a Home Assistant Repair card ([153fd38](https://github.com/tgenov/ha-iletcomfort/commit/153fd38426c764c883a0efd3b185e20d8c89926e))

## [0.2.1](https://github.com/tgenov/ha-iletcomfort/compare/v0.2.0...v0.2.1) (2026-05-26)


### Bug Fixes

* handle truncated C3 frames and stop warning spam ([#5](https://github.com/tgenov/ha-iletcomfort/issues/5)) ([5c642be](https://github.com/tgenov/ha-iletcomfort/commit/5c642be5bf5ef5044103e210448590b68bca41fc))
* handle truncated C3 frames and stop warning spam ([#5](https://github.com/tgenov/ha-iletcomfort/issues/5)) ([8d4900f](https://github.com/tgenov/ha-iletcomfort/commit/8d4900f10ae2fd318a84be2e632eff821997618a))
* reject 14-byte sensor frames and stop cache-fallback warning spam ([f0ead5d](https://github.com/tgenov/ha-iletcomfort/commit/f0ead5d7e7fceb40c196e2358ecf2c5c8c3311d1))
* require full sensor data block before accepting a frame ([8f30a10](https://github.com/tgenov/ha-iletcomfort/commit/8f30a10c9a0ebc4e65bbf2684744964139ac90af))
