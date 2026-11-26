#!/usr/bin/env node
'use strict';

const path = require('path');
const { main } = require(
  path.join(__dirname, '../../../shared/batches/BATCH-P2P-0002/probe-core.js'),
);

process.exitCode = main('target');
