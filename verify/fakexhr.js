// Offline: serve captured SwOS sections to the real engine.js. Nothing leaves the page; POSTs are dropped.
(function () {
  function FakeXHR() { this.readyState = 0; this.status = 0; this.responseText = ''; this.response = null; }
  FakeXHR.prototype.open = function (m, u) { this.m = m; this.u = String(u).replace(/^.*\/\//, '').replace(/^[^/]*\//, '').replace(/^\//, ''); };
  FakeXHR.prototype.setRequestHeader = function () {};
  FakeXHR.prototype.abort = function () { this.aborted = true; };
  FakeXHR.prototype.send = function () {
    var x = this;
    Promise.resolve().then(function () {
      if (x.aborted) return;
      x.readyState = 4;
      var d = window.SWOS_DATA[x.u];
      if (x.m === 'GET' && d !== undefined) { x.status = 200; x.responseText = d; x.response = d; }
      else { x.status = x.m === 'GET' ? 404 : 0; }
      if (x.onreadystatechange) x.onreadystatechange();
    });
  };
  window.XMLHttpRequest = FakeXHR;
})();
