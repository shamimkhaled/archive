(function () {
    var canvas = document.getElementById("sig-pad");
    if (!canvas) {
        return;
    }

    var ctx = canvas.getContext("2d");
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#1a1a1a";

    var clearButton = document.getElementById("sig-clear");
    var submitButton = document.getElementById("sig-submit");
    var dataInput = document.getElementById("sig-data");
    var form = document.getElementById("sig-form");

    var drawing = false;
    var hasDrawn = false;

    function pointFromEvent(event) {
        var rect = canvas.getBoundingClientRect();
        var point = event.touches ? event.touches[0] : event;
        return {
            x: point.clientX - rect.left,
            y: point.clientY - rect.top,
        };
    }

    function startStroke(event) {
        drawing = true;
        var p = pointFromEvent(event);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        event.preventDefault();
    }

    function moveStroke(event) {
        if (!drawing) {
            return;
        }
        var p = pointFromEvent(event);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
        if (!hasDrawn) {
            hasDrawn = true;
            submitButton.disabled = false;
        }
        event.preventDefault();
    }

    function endStroke(event) {
        drawing = false;
        if (event) {
            event.preventDefault();
        }
    }

    canvas.addEventListener("mousedown", startStroke);
    canvas.addEventListener("mousemove", moveStroke);
    window.addEventListener("mouseup", endStroke);

    canvas.addEventListener("touchstart", startStroke, { passive: false });
    canvas.addEventListener("touchmove", moveStroke, { passive: false });
    canvas.addEventListener("touchend", endStroke, { passive: false });

    clearButton.addEventListener("click", function () {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        hasDrawn = false;
        submitButton.disabled = true;
    });

    form.addEventListener("submit", function () {
        dataInput.value = canvas.toDataURL("image/png");
    });
})();
