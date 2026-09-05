# Changelog

## [1.1.5](https://github.com/AustinWinstanley/trading-bot/compare/v1.1.4...v1.1.5) (2026-09-05)


### Bug Fixes

* **2x:** revert mom_ls averaging-down exemption ([bdd469c](https://github.com/AustinWinstanley/trading-bot/commit/bdd469c69b1abf28b1fc9b09a3df1a58e41a3c43))
* skip broker-inactive assets instead of daily CRITICAL ([ee8547b](https://github.com/AustinWinstanley/trading-bot/commit/ee8547b1cbc96e009fecaa9a567721a635bcf349))

## [1.1.4](https://github.com/AustinWinstanley/trading-bot/compare/v1.1.3...v1.1.4) (2026-08-24)


### Bug Fixes

* backfill a fallback stop for any held position that never got one ([#43](https://github.com/AustinWinstanley/trading-bot/issues/43)) ([9cda9dd](https://github.com/AustinWinstanley/trading-bot/commit/9cda9dd9e33381f3dcb2511e21d98acd27ec7cd2))

## [1.1.3](https://github.com/AustinWinstanley/trading-bot/compare/v1.1.2...v1.1.3) (2026-08-19)


### Bug Fixes

* give client_order_id a per-run time component to stop same-day collisions ([#39](https://github.com/AustinWinstanley/trading-bot/issues/39)) ([ff67713](https://github.com/AustinWinstanley/trading-bot/commit/ff6771352bb42b8148bf2d1692460a43343b45a2))

## [1.1.2](https://github.com/AustinWinstanley/trading-bot/compare/v1.1.1...v1.1.2) (2026-08-18)


### Bug Fixes

* correct deploy/upgrade.sh's flock -u syntax for releasing drained locks ([#37](https://github.com/AustinWinstanley/trading-bot/issues/37)) ([4398c18](https://github.com/AustinWinstanley/trading-bot/commit/4398c18360bcd2928aa5e15642b82f17bf8177f5))

## [1.1.1](https://github.com/AustinWinstanley/trading-bot/compare/v1.1.0...v1.1.1) (2026-08-18)


### Bug Fixes

* reject orders shrunk below min_order_notional instead of submitting them ([#35](https://github.com/AustinWinstanley/trading-bot/issues/35)) ([3be905f](https://github.com/AustinWinstanley/trading-bot/commit/3be905ff37019d5eb778d6db1b1854cb280b309f))

## [1.1.0](https://github.com/AustinWinstanley/trading-bot/compare/v1.0.4...v1.1.0) (2026-08-17)


### Features

* extend deploy/upgrade.sh to upgrade all four services, plus git pull ([#32](https://github.com/AustinWinstanley/trading-bot/issues/32)) ([b23926d](https://github.com/AustinWinstanley/trading-bot/commit/b23926d7fb250a1c167c9ad5fed18c0296818afc))

## [1.0.4](https://github.com/AustinWinstanley/trading-bot/compare/v1.0.3...v1.0.4) (2026-08-17)


### Bug Fixes

* correct deploy/crontab against the real production schedule ([#30](https://github.com/AustinWinstanley/trading-bot/issues/30)) ([acecdec](https://github.com/AustinWinstanley/trading-bot/commit/acecdec488c43b55508aee274165292b35f2158b))

## [1.0.3](https://github.com/AustinWinstanley/trading-bot/compare/v1.0.2...v1.0.3) (2026-08-14)


### Bug Fixes

* docker deploy audit findings (compose file baked in, upgrade verifies before switching) ([#28](https://github.com/AustinWinstanley/trading-bot/issues/28)) ([46c6790](https://github.com/AustinWinstanley/trading-bot/commit/46c67909fb617e49ea2367447cc0596cfb69220c))

## [1.0.2](https://github.com/AustinWinstanley/trading-bot/compare/v1.0.1...v1.0.2) (2026-08-14)


### Bug Fixes

* two options-experiment health-check false positives ([#26](https://github.com/AustinWinstanley/trading-bot/issues/26)) ([b7e2113](https://github.com/AustinWinstanley/trading-bot/commit/b7e21132ddc78296033aff550be0a76fa5d0e0b9))

## [1.0.1](https://github.com/AustinWinstanley/trading-bot/compare/v1.0.0...v1.0.1) (2026-08-14)


### Bug Fixes

* Dependabot commitlint compatibility + 3 action version bumps ([#21](https://github.com/AustinWinstanley/trading-bot/issues/21)) ([c681b3d](https://github.com/AustinWinstanley/trading-bot/commit/c681b3d6c8fc43b57b969eadcb296c1bfc48f8ac))
* **deps:** resolve pip-dependencies group conflicts from [#23](https://github.com/AustinWinstanley/trading-bot/issues/23) ([#24](https://github.com/AustinWinstanley/trading-bot/issues/24)) ([88ff7d6](https://github.com/AustinWinstanley/trading-bot/commit/88ff7d621d03338775da7d67edae2afb8af1f5dd))

## 1.0.0 (2026-08-14)


### Features

* containerize the engine + scheduler, retiring host crontab ([c1a3efd](https://github.com/AustinWinstanley/trading-bot/commit/c1a3efdcb698030257825fd02e3a50774d7b5930))


### Bug Fixes

* bootstrap release-please at 0.1.0, not the 1.0.0 default ([3c220d7](https://github.com/AustinWinstanley/trading-bot/commit/3c220d75e548ed18a5d1782b63204d095cdf210a))
* read SEC EDGAR User-Agent from SEC_USER_AGENT ([6999b30](https://github.com/AustinWinstanley/trading-bot/commit/6999b30d267ea0ecc1de3027731b5a03c033dea3))
* rename commitlint.config.js to .cjs to fix CI ([d815819](https://github.com/AustinWinstanley/trading-bot/commit/d815819a831414181b4cb954cf4c93c942ba8b4d))
