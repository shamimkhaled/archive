(function () {
    var canvas = document.getElementById("sig-pad");
    if (!canvas) {
        return;
    }

    var ctx = canvas.getContext("2d");
    var clearButton = document.getElementById("sig-clear");
    var submitButton = document.getElementById("sig-submit");
    var dataInput = document.getElementById("sig-data");
    var form = document.getElementById("sig-form");
    var hint = document.getElementById("sig-hint");

    var strokes = [];
    var current = null;
    var activePointer = null;
    var submitting = false;

    function setupPen() {
        ctx.lineWidth = 2.4;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = "#1a1a1a";
    }

    function redraw() {
        var ratio = canvas.width / Math.max(1, canvas.clientWidth);
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        setupPen();
        strokes.forEach(function (stroke) {
            if (!stroke.length) {
                return;
            }
            ctx.beginPath();
            ctx.moveTo(stroke[0].x, stroke[0].y);
            if (stroke.length === 1) {
                ctx.lineTo(stroke[0].x + 0.1, stroke[0].y + 0.1);
            }
            for (var i = 1; i < stroke.length; i++) {
                ctx.lineTo(stroke[i].x, stroke[i].y);
            }
            ctx.stroke();
        });
    }

    function resize() {
        var width = canvas.clientWidth;
        var height = canvas.clientHeight;
        if (!width || !height) {
            return;
        }
        var dpr = Math.min(window.devicePixelRatio || 1, 3);
        var targetW = Math.round(width * dpr);
        var targetH = Math.round(height * dpr);
        if (canvas.width === targetW && canvas.height === targetH) {
            return;
        }
        var oldWidth = canvas._cssWidth || width;
        var scale = width / oldWidth;
        if (scale !== 1) {
            strokes = strokes.map(function (stroke) {
                return stroke.map(function (p) {
                    return { x: p.x * scale, y: p.y * scale };
                });
            });
        }
        canvas._cssWidth = width;
        canvas.width = targetW;
        canvas.height = targetH;
        redraw();
    }

    function pointFrom(event) {
        var rect = canvas.getBoundingClientRect();
        return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function updateState() {
        var hasInk = strokes.some(function (s) { return s.length > 0; });
        submitButton.disabled = !hasInk || submitting;
        if (hint) {
            hint.classList.toggle("is-hidden", hasInk);
        }
    }

    canvas.addEventListener("pointerdown", function (event) {
        if (activePointer !== null || submitting) {
            return;
        }
        activePointer = event.pointerId;
        try { canvas.setPointerCapture(event.pointerId); } catch (e) { /* older browsers */ }
        current = [pointFrom(event)];
        strokes.push(current);
        redraw();
        updateState();
        event.preventDefault();
    });

    canvas.addEventListener("pointermove", function (event) {
        if (event.pointerId !== activePointer || !current) {
            return;
        }
        var events = event.getCoalescedEvents ? event.getCoalescedEvents() : [event];
        var last = current[current.length - 1];
        ctx.beginPath();
        ctx.moveTo(last.x, last.y);
        events.forEach(function (e) {
            var p = pointFrom(e);
            current.push(p);
            ctx.lineTo(p.x, p.y);
        });
        ctx.stroke();
        event.preventDefault();
    });

    function endStroke(event) {
        if (event.pointerId !== activePointer) {
            return;
        }
        activePointer = null;
        current = null;
    }
    canvas.addEventListener("pointerup", endStroke);
    canvas.addEventListener("pointercancel", endStroke);

    clearButton.addEventListener("click", function () {
        strokes = [];
        current = null;
        activePointer = null;
        redraw();
        updateState();
    });

    form.addEventListener("submit", function (event) {
        if (submitting || !strokes.length) {
            event.preventDefault();
            return;
        }
        submitting = true;
        dataInput.value = canvas.toDataURL("image/png");
        submitButton.disabled = true;
        clearButton.disabled = true;
        submitButton.textContent = "Signing in…";
    });

    if (window.ResizeObserver) {
        var observer = new ResizeObserver(function () {
            if (!canvas.isConnected) {
                observer.disconnect();
                return;
            }
            resize();
        });
        observer.observe(canvas);
    } else {
        window.addEventListener("resize", resize);
    }
    resize();
    updateState();
})();
