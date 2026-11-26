'use strict';

const path = require('path');

function core(repo) {
    return require(path.join(repo, 'packages', 'core', 'src', 'js-joda.js'));
}

function check(repo) {
    const joda = core(repo);
    const observed = {
        localTime: joda.LocalTime.parse('12:30').toString(),
        yearMonth: joda.YearMonth.parse('2020-06').toString(),
        plusDays: joda.LocalDate.parse('2020-02-29').plusDays(1).toString(),
        duration90: joda.Duration.ofSeconds(90).toString(),
        epochInstant: joda.Instant.parse('1970-01-01T00:00:00Z').toString(),
    };
    const expected = {
        localTime: '12:30',
        yearMonth: '2020-06',
        plusDays: '2020-03-01',
        duration90: 'PT1M30S',
        epochInstant: '1970-01-01T00:00:00Z',
    };
    const ok = Object.keys(expected).every((key) => observed[key] === expected[key]);
    return { ok, value: observed };
}

module.exports = { check };
