CTFd._internal.challenge.data = undefined

CTFd._internal.challenge.renderer = CTFd._internal.markdown;


CTFd._internal.challenge.preRender = function() {}

CTFd._internal.challenge.render = function(markdown) {

    return CTFd._internal.challenge.renderer.parse(markdown)
}


CTFd._internal.challenge.postRender = function() {
    get_docker_status();
    createWarningModalBody();
}

function createWarningModalBody(){
    // Creates the Warning Modal placeholder, that will be updated when stuff happens.
    if (CTFd.lib.$('#warningModalBody').length === 0) {
        CTFd.lib.$('body').append('<div id="warningModalBody"></div>');
    }
}

function docker_chal_id() {
    return CTFd._internal.challenge.data.id;
}

function docker_start_button_html() {
    return `
        <span>
            <a onclick="start_container();" class='btn btn-dark'>
                <small style='color:white;'><i class="fas fa-play"></i>  Start Docker Instance for challenge</small>
            </a>
        </span>`;
}

function get_docker_status() {
    const chalId = docker_chal_id();
    // Default: show the start button. Replaced below if a container is running.
    CTFd.lib.$('#docker_container').html(docker_start_button_html());

    CTFd.fetch("/api/v1/docker_status").then(response => response.json())
    .then(result => {
        (result.data || []).forEach(item => {
            if (String(item.challenge_id) !== String(chalId)) {
                return;
            }
            var ports = (item.ports || []).map(String);
            var data = '';
            ports.forEach(port => {
                data = data + 'Host: ' + item.host + ' Port: ' + port + '<br />';
            });
            var revertId = 'revert_' + String(item.instance_id).substring(0, 10);
            CTFd.lib.$('#docker_container').html('<pre>Docker Container Information:<br />' + data +
                '<div class="mt-2" id="' + revertId + '"></div></pre>');

            // Fill "host" / "port" placeholders in the challenge's connection info
            var $link = CTFd.lib.$('.challenge-connection-info');
            if ($link.length) {
                var html = $link.html();
                html = html.replace(/\bhost\b/gi, item.host);
                html = html.replace(/\bport\b|\b\d{5}\b/gi, ports[0]);
                $link.html(html);
                $link.each(function () {
                    const $span = CTFd.lib.$(this);
                    const h = $span.html();
                    if (h.includes("<a")) { return; }
                    const urlMatch = h.match(/(http[s]?:\/\/[^\s<]+)/);
                    if (urlMatch) {
                        const url = urlMatch[0];
                        $span.html(h.replace(url, `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`));
                    }
                });
            }

            // Stop is available right away; Revert after the countdown
            var stopBtn = '<a onclick="stop_container();" class="btn btn-danger">' +
                '<small style="color:white;"><i class="fas fa-stop"></i> Stop</small></a>';
            var revertBtn = '<a onclick="start_container();" class="btn btn-dark">' +
                '<small style="color:white;"><i class="fas fa-redo"></i> Revert</small></a> ';
            var countDownDate = new Date(parseInt(item.revert_time) * 1000).getTime();
            var tick = function() {
                var distance = countDownDate - new Date().getTime();
                if (distance <= 0) {
                    clearInterval(x);
                    CTFd.lib.$("#" + revertId).html(revertBtn + stopBtn);
                    return;
                }
                var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
                var seconds = Math.floor((distance % (1000 * 60)) / 1000);
                if (seconds < 10) { seconds = "0" + seconds; }
                CTFd.lib.$("#" + revertId).html(stopBtn +
                    '<div class="mt-1"><small>Revert available in ' + minutes + ':' + seconds + '</small></div>');
            };
            var x = setInterval(tick, 1000);
            tick();
        });
    })
    .catch(error => {
        console.error('Error fetching docker status:', error);
    });
}

function stop_container() {
    if (confirm("Are you sure you want to stop the container for: \n" + CTFd._internal.challenge.data.name)) {
        CTFd.fetch("/api/v1/container", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify({ id: docker_chal_id(), stop: true })
        })
        .then(function (response) {
            return response.json().then(function (json) {
                if (response.ok) {
                    updateWarningModal({
                        title: "Attention!",
                        warningText: "The Docker container for <br><strong>" + CTFd._internal.challenge.data.name + "</strong><br> was stopped successfully.",
                        buttonText: "Close",
                        meme: "stopped",
                        onClose: function () { get_docker_status(); }
                    });
                } else {
                    throw new Error(json.message || 'Failed to stop container');
                }
            });
        })
        .catch(function (error) {
            updateWarningModal({
                title: "Error",
                warningText: error.message || "An unknown error occurred while stopping the container.",
                buttonText: "Close",
                meme: docker_meme_for(error.message),
                onClose: function () { get_docker_status(); }
            });
        });
    }
}

function start_container() {
    CTFd.lib.$('#docker_container').html('<div class="text-center"><i class="fas fa-circle-notch fa-spin fa-1x"></i></div>');
    CTFd.fetch("/api/v1/container", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ id: docker_chal_id() })
    }).then(function (response) {
        return response.json().then(function (json) {
            if (response.ok) {
                get_docker_status();
                updateWarningModal({
                    title: "Attention!",
                    warningText: "A Docker container is started for you.<br>You can stop it any time. Revert unlocks after a few minutes.",
                    buttonText: "Got it!",
                    meme: "started"
                });
            } else {
                throw new Error(json.message || 'Failed to start container');
            }
        });
    }).catch(function (error) {
        updateWarningModal({
            title: "Error!",
            warningText: error.message || "An unknown error occurred when starting your Docker container.",
            buttonText: "Got it!",
            meme: docker_meme_for(error.message),
            onClose: function () { get_docker_status(); }
        });
    });
}

// Optional images for the popups: drop e.g. memes/cooldown.png into the plugin's assets.
var __dockerMemes = null;
function docker_memes() {
    if (!__dockerMemes) {
        __dockerMemes = CTFd.fetch("/api/v1/docker_memes")
            .then(function (r) { return r.json(); })
            .then(function (j) { return j.data || {}; })
            .catch(function () { return {}; });
    }
    return __dockerMemes;
}

function docker_meme_for(message) {
    var m = String(message || "").toLowerCase();
    if (m.indexOf("please wait") !== -1) return "cooldown";
    if (m.indexOf("only run one container") !== -1) return "one-at-a-time";
    if (m.indexOf("revert unlocks") !== -1) return "revert-wait";
    if (m.indexOf("still running") !== -1) return "busy";
    return "error";
}

// WE NEED TO CREATE THE MODAL FIRST, and this should be only used to fill it.

var __warnCountdown = null;

function updateWarningModal({
    title , warningText, buttonText, onClose, meme } = {}) {
    clearInterval(__warnCountdown);
    // "Please wait 30 seconds ..." -> live countdown 30, 29, 28 ...
    var waitLeft = null;
    var waitMatch = String(warningText || "").match(/(wait\s+)(\d+)(\s+seconds?)/i);
    if (waitMatch) {
        waitLeft = parseInt(waitMatch[2], 10);
        if (/^error/i.test(String(title))) title = "Cooling down";
        warningText = '<span id="warningCountdownMsg">' + String(warningText).replace(waitMatch[0],
            waitMatch[1] + '<strong id="warningCountdown" style="font-variant-numeric:tabular-nums">' + waitLeft + '</strong><span id="warningCountdownUnit">' + waitMatch[3] + '</span>') + '</span>';
    }
    const modalHTML = `
        <div id="warningModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; z-index:9999; background-color:rgba(0,0,0,0.5);">
          <div style="position:relative; margin:10% auto; width:400px; background:white; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,0.3); overflow:hidden;">
            <div class="modal-header bg-warning text-dark" style="padding:1rem; display:flex; justify-content:space-between; align-items:center;">
              <h5 class="modal-title" style="margin:0;">${title}</h5>
              <button type="button" id="warningCloseBtn" style="border:none; background:none; font-size:1.5rem; line-height:1; cursor:pointer;">&times;</button>
            </div>
            <div class="modal-body" style="padding:1rem; color:#212529;">
              <div id="warningMeme" class="text-center"></div>
              ${warningText}
            </div>
            <div class="modal-footer" style="padding:1rem; text-align:right; border-top:1px solid #dee2e6;">
              <button type="button" class="btn btn-secondary" id="warningOkBtn">${buttonText}</button>
            </div>
          </div>
        </div>
    `;
    CTFd.lib.$("#warningModalBody").html(modalHTML);

    // Show the modal
    CTFd.lib.$("#warningModal").show();

    // Close logic with callback
    if (waitLeft !== null) {
        var waitEnd = Date.now() + waitLeft * 1000;
        __warnCountdown = setInterval(function () {
            var left = Math.max(0, Math.ceil((waitEnd - Date.now()) / 1000));
            var el = document.getElementById("warningCountdown");
            if (!el) { clearInterval(__warnCountdown); return; }
            el.textContent = left;
            var unit = document.getElementById("warningCountdownUnit");
            if (unit) unit.textContent = left === 1 ? " second" : " seconds";
            if (left === 0) {
                clearInterval(__warnCountdown);
                var msg = document.getElementById("warningCountdownMsg");
                if (msg) msg.innerHTML = "<strong>Ready!</strong> You can start a new container now.";
            }
        }, 250);
    }

    const closeModal = () => {
        clearInterval(__warnCountdown);
        CTFd.lib.$("#warningModal").hide();
        if (typeof onClose === 'function') {
            onClose();  
        }
    };

    CTFd.lib.$("#warningCloseBtn").on("click", closeModal);
    CTFd.lib.$("#warningOkBtn").on("click", closeModal);

    if (meme) {
        docker_memes().then(function (memes) {
            if (!memes[meme]) return;
            var img = document.createElement("img");
            img.src = memes[meme];
            img.alt = "";
            img.style.cssText = "max-width:100%; max-height:220px; margin-bottom:0.75rem; border-radius:4px;";
            img.onerror = function () { img.remove(); };
            var slot = document.getElementById("warningMeme");
            if (slot) slot.appendChild(img);
        });
    }
}

// In order to capture the flag submission, and remove the "Revert" and "Stop" buttons after solving a challenge
// We need to hook that call, and do this manually.
function checkForCorrectFlag() {
    const challengeWindow = document.querySelector('#challenge-window');
    if (!challengeWindow || getComputedStyle(challengeWindow).display === 'none') {
        // console.log("❌ Challenge window hidden or closed, stopping check.");
        clearInterval(checkInterval);
        checkInterval = null;
        return;
    }

    const notification = document.querySelector('.notification-row .alert');
    if (!notification) return;

    const strong = notification.querySelector('strong');
    if (!strong) return;

    const message = strong.textContent.trim();

    if (message.includes("Correct")) {
        // console.log("✅ Correct flag detected:", message);
        get_docker_status();
        clearInterval(checkInterval);
        checkInterval = null;
    }
}

if (!checkInterval) {
    var checkInterval = setInterval(checkForCorrectFlag, 1500);
}
