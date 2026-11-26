'use strict';

const path = require('path');

function core(repo) {
    return require(path.join(repo, 'packages', 'core', 'src', 'js-joda.js'));
}

function check(repo) {
    const joda = core(repo);
    const observed = {};

    const localDate = joda.LocalDate.of(2020, 1, 1);
    const localDateBefore = localDate.toString();
    const localDateNext = localDate.plusDays(1);
    observed.localDateReturnsNewInstance = localDateNext !== localDate;
    observed.localDateReceiverUnchanged = localDate.toString() === localDateBefore;

    const instant = joda.Instant.parse('2020-01-01T00:00:00Z');
    const instantBefore = instant.toString();
    const instantNext = instant.plusSeconds(60);
    observed.instantReturnsNewInstance = instantNext !== instant;
    observed.instantReceiverUnchanged = instant.toString() === instantBefore;

    const zoned = joda.ZonedDateTime.parse('2020-01-01T00:00:00Z');
    const zonedBefore = zoned.toString();
    const zonedNext = zoned.plusDays(1);
    observed.zonedReturnsNewInstance = zonedNext !== zoned;
    observed.zonedReceiverUnchanged = zoned.toString() === zonedBefore;

    const precise = joda.Instant.parse('2020-01-01T00:00:00.500Z');
    const preciseBefore = precise.toString();
    const truncated = precise.truncatedTo(joda.ChronoUnit.SECONDS);
    observed.truncateReturnsNewInstance = truncated !== precise;
    observed.truncateReceiverUnchanged = precise.toString() === preciseBefore;

    const ok = Object.keys(observed).every((key) => observed[key] === true);
    return { ok, value: observed };
}

module.exports = { check };
