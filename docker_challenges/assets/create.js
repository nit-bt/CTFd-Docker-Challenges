CTFd.plugin.run((_CTFd) => {
    const $ = _CTFd.lib.$;
    const md = _CTFd.lib.markdown();
    $('a[href="#new-desc-preview"]').on('shown.bs.tab', function (event) {
        if (event.target.hash == '#new-desc-preview') {
            $(event.target.hash).html(md.render($('#new-desc-editor').val()));
        }
    });
    $.getScript("/plugins/docker_challenges/assets/docker_admin_common.js", function () {
        $(document).ready(function () { dockerLoadHosts($, null, null); });
    });
});
