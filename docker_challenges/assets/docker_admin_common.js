// Shared by create.js / update.js: fill host + image dropdowns.
function dockerLoadImages($, hostId, selected) {
    var $img = $("#dockerimage_select");
    $img.empty().prop("disabled", false);
    if (!hostId) {
        $img.append($("<option />").val("").text("Pick a host first"));
        return;
    }
    $.getJSON("/api/v1/docker?host_id=" + encodeURIComponent(hostId))
        .done(function (result) {
            $.each(result.data, function (i, item) {
                $img.append($("<option />").val(item.name).text(item.name));
            });
            if (selected) { $img.val(selected); }
        })
        .fail(function (xhr) {
            var msg = (xhr.responseJSON && xhr.responseJSON.data) ? xhr.responseJSON.data[0].name : "Error loading images";
            $img.append($("<option />").val("").text(msg)).prop("disabled", true);
        });
}

function dockerLoadHosts($, selectedHost, selectedImage) {
    var $host = $("#dockerhost_select");
    $.getJSON("/api/v1/docker_hosts", function (result) {
        $host.empty();
        if (!result.data.length) {
            $host.append($("<option />").val("").text("No hosts. Add one in Docker Config"));
            return;
        }
        $host.append($("<option />").val("").text("-- select host --"));
        $.each(result.data, function (i, h) {
            $host.append($("<option />").val(h.id).text(h.name + " (" + h.public_host + ")"));
        });
        if (selectedHost) { $host.val(String(selectedHost)); }
        dockerLoadImages($, $host.val(), selectedImage);
    });
    $host.on("change", function () { dockerLoadImages($, $(this).val(), null); });
}
