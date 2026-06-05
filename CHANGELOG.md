# Changelog

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
