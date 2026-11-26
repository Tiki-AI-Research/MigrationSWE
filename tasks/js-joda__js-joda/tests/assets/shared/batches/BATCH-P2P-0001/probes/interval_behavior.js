'use strict';

const path = require('path');

function core(repo) {
    return require(path.join(repo, 'packages', 'core', 'src', 'js-joda.js'));
}

function extra(repo) {
    return require(path.join(repo, 'packages', 'extra', 'src', 'js-joda-extra.js'));
}

function check(repo) {
    const joda = core(repo);
    const jodaExtra = extra(repo);

    const start = joda.Instant.parse('2020-01-01T00:00:00Z');
    const end = joda.Instant.parse('2020-02-01T00:00:00Z');
    const interval = jodaExtra.Interval.of(start, end);
    const observed = {
        interval: interval.toString(),
        containsMiddle: interval.contains(joda.Instant.parse('2020-01-15T00:00:00Z')),
        containsEnd: interval.contains(end),
    };
    const ok = typeof observed.interval === 'string'
        && observed.interval.length > 0
        && observed.containsMiddle === true
        && observed.containsEnd === false;
    return { ok, value: observed };
}

module.exports = { check };
