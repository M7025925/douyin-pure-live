const fs = require('fs');

globalThis.window = globalThis;
globalThis.navigator = { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0', appName: 'Netscape', platform: 'Win32',
  language: 'zh-CN', cookieEnabled: true, plugins: [], mimeTypes: [] };
globalThis.location = { href: 'https://live.douyin.com/', protocol: 'https:',
  host: 'live.douyin.com', hostname: 'live.douyin.com', port: '',
  pathname: '/', search: '', hash: '', origin: 'https://live.douyin.com' };
globalThis.screen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
  colorDepth: 24, pixelDepth: 24 };
globalThis.history = { pushState: function(){}, replaceState: function(){},
  back: function(){}, forward: function(){}, go: function(){} };
globalThis.performance = { now: function(){ return Date.now(); }, timing: {},
  getEntries: function(){ return []; } };
var __stubStorage = { getItem: function(){ return null; }, setItem: function(){},
  removeItem: function(){}, clear: function(){} };
globalThis.localStorage = __stubStorage;
globalThis.sessionStorage = __stubStorage;
var document = {
  readyState: 'complete', cookie: '', referrer: '', title: '',
  createElement: function(tag) { return { tag: tag, style: {}, href: '', rel: '',
    type: '', charset: '', async: true, readyState: 'complete',
    sheet: { cssRules: [], insertRule: function(){}, addRule: function(){} },
    appendChild: function(){}, removeChild: function(){}, insertBefore: function(){},
    setAttribute: function(){}, getAttribute: function(){ return null; },
    addEventListener: function(){}, attachEvent: function(){},
    contentWindow: { postMessage: function(){} },
    parentNode: { removeChild: function(){} } }; },
  createTextNode: function() { return {}; },
  getElementsByTagName: function() { return [document.head]; },
  documentElement: { style: {}, readyState: 'complete',
    addEventListener: function(){}, attachEvent: function(){},
    getAttribute: function(){ return null; }, setAttribute: function(){} },
  head: { appendChild: function(){}, removeChild: function(){}, insertBefore: function(){} },
  body: { appendChild: function(){}, removeChild: function(){}, insertBefore: function(){} },
  addEventListener: function(){}, attachEvent: function(){},
  removeEventListener: function(){}, detachEvent: function(){},
  getElementById: function(){ return null; },
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; },
  createEvent: function() { return { initEvent: function(){} }; },
  createObjectURL: function(){ return ''; }
};
globalThis.document = document;
window.addEventListener = function(){}; window.attachEvent = function(){};
window.removeEventListener = function(){}; window.detachEvent = function(){};
window.requestAnimationFrame = function(cb){ return 0; };
window.getComputedStyle = function(){ return { getPropertyValue: function(){ return ''; } }; };

eval(fs.readFileSync(process.argv[2], 'utf8'));
const r = module.exports.frontierSign({'X-MS-STUB': process.argv[3]});
process.stdout.write(r['X-Bogus']);
process.exit(0);
