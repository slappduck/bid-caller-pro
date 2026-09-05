"""app.html has to survive being loaded, not just parsed.

`node --check` reports valid syntax and says nothing about whether the script
runs. That gap shipped a real bug in this file: a const declared beside the
functions that used it, while a Set built six hundred lines earlier named it.
A const does not hoist, so the page threw a ReferenceError at load and took the
entire app down with it. The syntax check passed.

So this executes the top level under a minimal DOM and fails on any error
raised before the first user interaction. It is not a browser and does not
pretend to be one: it catches exactly the class of fault that kills the page
on load, which is the class that costs the most.

An earlier version of this file also carried a regex scan for
use-before-declaration. It flagged four names, every one of them a false
positive -- three matched the prose of the comments above, the fourth matched
a legal forward reference inside a function body. A scan that cannot tell a
comment from code is not evidence. It is replaced here by a canary: the same
harness is re-run against a deliberately broken copy of the script, and the
test fails unless the harness reports the break. That proves the load test can
still see the fault it exists to catch.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.html")

SHIM = r"""
const noop = function () {};
function el() {
  return {style:{}, classList:{add:noop,remove:noop,toggle:noop,contains:()=>false},
          dataset:{}, setAttribute:noop, getAttribute:()=>null, appendChild:noop,
          removeChild:noop, addEventListener:noop, removeEventListener:noop,
          replaceWith:noop, remove:noop, focus:noop, click:noop, innerHTML:"",
          textContent:"", value:"", checked:false, disabled:false, children:[],
          querySelector:()=>null, querySelectorAll:()=>[],
          getBoundingClientRect:()=>({top:0,bottom:0,left:0,right:0,width:0,height:0})};
}
global.window = global;
global.self = global;
global.document = {
  getElementById: () => el(), querySelector: () => el(), querySelectorAll: () => [],
  createElement: () => el(), createElementNS: () => el(),
  addEventListener: noop, removeEventListener: noop,
  head: el(), body: el(), documentElement: el(),
  readyState: "complete", cookie: "", title: "",
};
global.addEventListener = noop;
global.removeEventListener = noop;
global.localStorage = {_d:{}, getItem(k){return this._d[k]??null;},
  setItem(k,v){this._d[k]=String(v);}, removeItem(k){delete this._d[k];},
  clear(){this._d={};}, key(i){return Object.keys(this._d)[i]??null;},
  get length(){return Object.keys(this._d).length;}};
global.sessionStorage = global.localStorage;
global.navigator = {onLine:true, userAgent:"node", geolocation:{getCurrentPosition:noop},
                    serviceWorker:{register:()=>Promise.resolve()}, clipboard:{writeText:()=>Promise.resolve()}};
global.location = {pathname:"/app.html", href:"https://curbcallpro.com/app.html",
                   origin:"https://curbcallpro.com", search:"", hash:"", reload:noop, replace:noop};
global.history = {replaceState:noop, pushState:noop};
global.fetch = () => Promise.resolve({ok:true, status:200, json:()=>Promise.resolve({}), text:()=>Promise.resolve("")});
global.matchMedia = () => ({matches:false, addEventListener:noop, addListener:noop});
global.requestAnimationFrame = (f) => setTimeout(f, 0);
global.Notification = {permission:"default", requestPermission:()=>Promise.resolve("default")};
global.IntersectionObserver = function(){ return {observe:noop, unobserve:noop, disconnect:noop}; };
global.URL = global.URL || function(){}; global.URL.createObjectURL = () => "blob:x";
global.URL.revokeObjectURL = noop;
global.Blob = function(){};
global.alert = noop; global.confirm = () => true; global.prompt = () => null;
"""


class AppTopLevelRunsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        with open(APP, encoding="utf-8") as f:
            html = f.read()
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            html, re.S)
        cls.script = "\n".join(blocks)

    def _load(self, script):
        """Run `script` under the shim. Returns "OK" or "THREW:<Name>: <msg>".

        The script goes through a file, not through argv: app.html's inline
        JavaScript is far past the kernel's limit on a single argument, and
        node exits with E2BIG before it reads a byte.
        """
        runner = (SHIM + "\ntry{ (0,eval)(" + json.dumps(script) +
                  "); console.log('OK'); }"
                  "catch(e){ console.log('THREW:'+e.constructor.name+': '+e.message); }")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "load.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(runner)
            out = subprocess.run([self.node, path], capture_output=True,
                                 text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        return ((out.stdout or "").strip().splitlines() or [""])[-1]

    def test_the_script_reaches_the_end_without_throwing(self):
        self.assertEqual(self._load(self.script), "OK",
                         "app.html threw while loading")

    def test_the_harness_still_catches_a_use_before_declaration(self):
        """Break the script on purpose; the load test has to notice.

        A passing load test is only worth anything if it would fail on the
        bug it was written for. This reintroduces that bug -- a reference to
        a top-level const placed above its declaration -- and requires the
        same harness to report a ReferenceError.
        """
        m = re.search(r"^const ([A-Za-z_$][\w$]*)\s*=", self.script, re.M)
        self.assertIsNotNone(m, "no top-level const found to break")
        name = m.group(1)
        verdict = self._load("void " + name + ";\n" + self.script)
        self.assertTrue(
            verdict.startswith("THREW:ReferenceError"),
            "harness did not catch a deliberate TDZ error on %s; got %r"
            % (name, verdict))
