// Minimal MV3 service worker — exists so the extension is observable as a
// CDP target, which makes "did the extension load" a checkable fact.
chrome.runtime.onInstalled.addListener(() => {});
self.__roxyNoiseVersion = '1.0.0';
