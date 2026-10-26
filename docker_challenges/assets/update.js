CTFd.plugin.run((_CTFd) => {
    const $ = _CTFd.lib.$;
    $.getScript("/plugins/docker_challenges/assets/docker_admin_common.js", function () {
        $(document).ready(function () { dockerLoadHosts($, DOCKER_HOST_ID, DOCKER_IMAGE); });
    });
});
