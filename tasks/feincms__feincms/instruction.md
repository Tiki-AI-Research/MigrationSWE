# Migrate FeinCMS from Django 3.2 to Django 5.2

## Background

FeinCMS is a Django-based page CMS and CMS building toolkit. Its repository still targets a Django baseline older than 5.2. You are responsible for locating every affected declaration and code path yourself.

## Goal

- Raise the supported Django baseline for FeinCMS from 3.2 to 5.2.
- No Django version below 5.2 may remain declared, referenced, or tested anywhere in the repository.
- Production code within the migration scope MUST rely on Django 5.2 behavior directly and MUST NOT retain runtime compatibility branches for Django versions below 5.2.
- The Python support range MUST stay at Python 3.10 through 3.13.

## Public contract to preserve

- `feincms.settings` MUST keep resolving `FEINCMS_*` settings through `feincms.LazySettings` against `django.conf.settings`.
- `feincms.admin.ParentFieldListFilter` and `feincms.admin.CategoryFieldListFilter` MUST remain registered on `django.contrib.admin.filters.FieldListFilter` for fields exposing `parent_filter` and `category_filter`.
- `feincms.extensions.datepublisher.granular_now` MUST keep its signature `granular_now(n=None, default_tz=None)` and MUST return a timezone-aware `datetime`.
- The datepublisher response processor MUST keep deriving response expiry headers from the page's publication end date.
- The public admin classes `feincms.admin.item_editor.ItemEditor` and `feincms.admin.tree_editor.TreeEditor` MUST keep their fieldset/tab and `list_filter` selection behavior.
- Content types MUST keep the `render(**kwargs)` contract.

## Behavior requirements

- When a `ParentFieldListFilter` or `CategoryFieldListFilter` renders with a single selected value, that choice MUST be reported as selected.
- When such a filter renders with multiple selected values, every selected choice MUST be reported as selected.
- `granular_now(d)` MUST return a timezone-aware datetime that keeps the date and hour of `d`, floors the minute to the previous five-minute boundary, and sets seconds to zero.
- `granular_now` MUST resolve a DST-ambiguous local time without raising an exception.
- Page 404 handling through `FEINCMS_CMS_404_PAGE` and preview routing MUST keep their current responses.
- The tree editor ordering `Meta.ordering = ["tree_id", "lft"]` MUST be preserved.

## Constraints

1. Production code within the migration scope MUST NOT branch at runtime on Django versions below 5.2.
2. The migration MUST NOT remove required public capabilities or change their public contracts outside the stated goal.
3. Changes MUST NOT alter the persisted database schema or stored records; existing migrations referenced through `MIGRATION_MODULES` MUST remain valid.
4. Repository template files and repository HTML, CSS, and JavaScript assets MUST remain byte-for-byte unchanged, except for the following two required documentation-only replacements in existing JavaScript comments:
   - In `feincms/static/feincms/item_editor.js`, replace `// Use Django's built-in inline spawing mechanism (Django 1.2+)` with `// Use Django's built-in inline spawing mechanism`, preserving the existing indentation and line ending.
   - In `feincms/static/feincms/tree_editor.js`, replace `// https://docs.djangoproject.com/en/1.4/ref/contrib/csrf/` with `// https://docs.djangoproject.com/en/5.2/ref/csrf/`, preserving the existing indentation and line ending.
   These are the only exceptions to the asset freeze and are required to satisfy the repository-wide version-reference requirement. No executable JavaScript, runtime string literal, HTML, CSS, template structure, event handling, or other asset byte may change. Leaving either obsolete reference in place does not satisfy the Goal.
5. Changes MUST NOT rename or remove public FeinCMS symbols.
6. The Python floor MUST NOT be raised above 3.10, and Python 3.10 and 3.11 MUST remain in the `tox` matrix.
7. Changes MUST be limited to the repository files required to raise the supported Django baseline; no unrelated files may be modified.
8. You MUST NOT skip checks, remove or weaken assertions, or special-case evaluation inputs to fake compliance.
9. Work in the repository working directory `/workspace/repo`.
10. The evaluation captures the repository changes as a patch against the migration baseline commit; keep all required changes present in the working tree.

## Validation

From `/workspace/repo`, run `tox`. This builds the configured environments and runs the `tests/testapp` suite with coverage, exercising page resolution, rendering, admin, and extension behavior. Run `tox -e e2e` for the Playwright end-to-end tests. These public checks exercise the listed behaviors but do not by themselves prove every requirement.

The migration baseline of this task is base commit 060eb148192aaec6aab394cf3001f1e5309bc548.
