'use strict';

const path = require('path');
const Module = require('module');

function aliasCore(repo) {
    const coreSrc = path.join(repo, 'packages', 'core', 'src', 'js-joda.js');
    if (!Module.__jodaCoreAliased) {
        const original = Module._resolveFilename;
        Module._resolveFilename = function (request) {
            if (request === '@js-joda/core') {
                return require.resolve(coreSrc);
            }
            return original.apply(this, arguments);
        };
        Module.__jodaCoreAliased = true;
    }
    return require(coreSrc);
}

function check(repo) {
    aliasCore(repo);
    const locale = require(path.join(repo, 'packages', 'locale', 'src', 'js-joda-locale.js'));
    const observed = {
        Locale: typeof locale.Locale !== 'undefined',
        WeekFields: typeof locale.WeekFields !== 'undefined',
        registerLocaleData: typeof locale.registerLocaleData === 'function',
    };
    let callable = false;
    if (observed.registerLocaleData) {
        try {
            locale.registerLocaleData('supplemental/likelySubtags.json', {});
            callable = true;
        } catch (e) {
            callable = false;
        }
    }
    observed.registerLocaleDataCallable = callable;
    const ok = observed.Locale && observed.WeekFields && observed.registerLocaleData && callable;
    return { ok, value: observed };
}

module.exports = { check };
