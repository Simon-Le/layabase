# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.0.0] - 2020-02-11
### Changed
- Update [marshmallow_sqlalchemy](https://marshmallow-sqlalchemy.readthedocs.io/en/latest/changelog.html) to version 0.22.*
- model schema now returns a SQLAlchemyAutoSchema instance instead of the deprecated ModelSchema.
- `required_on_query` should be set within `layabase` key inside info (was linked to `marshmallow` key previously).
- `allow_comparison_signs` should be set within `layabase` key inside info (was linked to `marshmallow` key previously).
- `interpret_star_character` should be set within `layabase` key inside info (was linked to `marshmallow` key previously).
- Iterate over SQLAlchemy fields to find the one required on queries instead of creating a Marshmallow schema.

## [3.5.1] - 2020-01-30
### Fixed
- [SQLAlchemy] Flask-RestPlus argument parsers (for GET and DELETE queries) are now restricting values in case the underlying field is of Enum type.

## [3.5.0] - 2020-01-07
### Changed
- Update [marshmallow_sqlalchemy](https://marshmallow-sqlalchemy.readthedocs.io/en/latest/changelog.html) to version 0.21.*

## [3.4.0] - 2019-12-02
### Added
- Initial release.

[Unreleased]: https://github.com/Colin-b/layabase/compare/v4.0.0...HEAD
[4.0.0]: https://github.com/Colin-b/layabase/compare/v3.5.1...v4.0.0
[3.5.1]: https://github.com/Colin-b/layabase/compare/v3.5.0...v3.5.1
[3.5.0]: https://github.com/Colin-b/layabase/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/Colin-b/layabase/releases/tag/v3.4.0
