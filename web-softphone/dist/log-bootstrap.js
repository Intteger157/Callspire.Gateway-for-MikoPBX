/** Runs before ES modules — JsSIP binds console.info at import time. */
(function () {
  if (window.__callspireLogs) return
  window.__callspireLogs = true

  var nextId = 1
  var boot = (window.__callspireLogBootstrap = [])

  function fmt(a) {
    if (a === undefined) return 'undefined'
    if (a === null) return 'null'
    if (typeof a === 'string') return a
    try { return JSON.stringify(a) } catch (e) { return String(a) }
  }

  function push(level, args) {
    boot.push({ id: nextId++, ts: Date.now(), level: level, text: Array.prototype.map.call(args, fmt).join(' ') })
    if (boot.length > 800) boot.splice(0, boot.length - 800)
  }

  var orig = {
    log: console.log.bind(console),
    info: console.info.bind(console),
    warn: console.warn.bind(console),
    error: console.error.bind(console),
    debug: console.debug.bind(console),
  }

  console.log = function () { push('log', arguments); orig.log.apply(console, arguments) }
  console.info = function () { push('info', arguments); orig.info.apply(console, arguments) }
  console.warn = function () { push('warn', arguments); orig.warn.apply(console, arguments) }
  console.error = function () { push('error', arguments); orig.error.apply(console, arguments) }
  console.debug = function () { push('debug', arguments); orig.debug.apply(console, arguments) }

  push('info', ['Early log capture (bootstrap)'])
})()
