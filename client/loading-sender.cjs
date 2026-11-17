"use strict";

function fileUrlPath(url) {
  return decodeURIComponent(url.pathname).replaceAll("\\", "/").toLowerCase();
}

function isTrustedLoadingUrl(rawUrl, loadingHref) {
  try {
    const actual = new URL(rawUrl);
    const expected = new URL(loadingHref);
    if (actual.protocol !== "file:" || expected.protocol !== "file:") return false;
    return fileUrlPath(actual) === fileUrlPath(expected);
  } catch (_) {
    return false;
  }
}

module.exports = { isTrustedLoadingUrl };
