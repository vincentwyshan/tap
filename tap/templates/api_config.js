

function saveApi(){
    var source = editor.getValue();
    $('[name=source]').val(source);

    var dbconn = [];
    for(var i=0; i< $('#dbconn').next().find('ul li.search-choice').size(); i++){
        var name = $($('#dbconn').next().find('ul li.search-choice')[i]).text();
        // dbconn.push($('#dbconn option:contains('+name+')').val());
        $('#dbconn option:contains('+name+')').each(function(){
            if($(this).text() == name){
                dbconn.push($(this).val());
            }
        });
    }
    $('[name=dbconn]').val(dbconn.join(','));

    var dbconn_secondary = [];
    for(var i=0; i< $('#dbconn_secondary').next().find('ul li.search-choice').size(); i++){
        var name = $($('#dbconn_secondary').next().find('ul li.search-choice')[i]).text();
        dbconn_secondary.push($('#dbconn_secondary option:contains('+name+')').val());
    }
    $('[name=dbconn_secondary]').val(dbconn_secondary.join(','));

    var data = $('#form-api').serialize();
    console.info(data);

    $('#btn-save-api').attr('disabled', true);

    function callback(response){
        if(response.success == 1){
            alert("保存成功");
        }
        else{
            alert(response.message);
        }
        $('#btn-save-api').removeAttr('disabled');
    }

    $.post('/management/action', data, callback, 'json');

    return false;
}


function newPara(){
    var api_id = $(this).attr('apiid');
    var data = {id:api_id, 'action':'paranew'};

    $('#btn-new-para').attr('disabled', true);

    function callback(response){
        if(response.success == 1){
            var html = $('#sample-para').html();
            html = html.replace(/{{para.id}}/g, response.para_id);
            html = html.replace(/{{para.name}}/g, response.para_name);
            html = html.replace(/{{para.default}}/g, '');
            html = $(html);
            html.removeAttr('id');
            html = $(html)
            $('#sample-para').before(html);
            html.show();
            html.find('.delete-para').click(delPara);
        }
        else {
            alert(response.message);
        }
        $('#btn-new-para').removeAttr('disabled');
    }

    $.post('/management/action', data, callback, 'json');
    return false;
}

function delPara(){
    var paraId = $(this).attr('paraid');
    var apiId = $('[apiid]').attr('apiid');
    var data = {id: paraId, action: 'paradelete', api_id: apiId}

    function callback(response){
        if(response.success == 1){
            $('[paraid='+paraId+']').parent().parent().remove();
        }
        else{
            alert(response.message);
        }
    }
    $.post('/management/action', data, callback, 'json');
    return false;
}

function apiTest(){
    $('#myModal').modal();

    var source = editor.getValue();
    $('[name=source]').val(source);

    var dbconn = [];
    for(var i=0; i< $('#dbconn').next().find('ul li.search-choice').size(); i++){
        var name = $($('#dbconn').next().find('ul li.search-choice')[i]).text();
        dbconn.push($('#dbconn option:contains('+name+')').val());
    }
    $('[name=dbconn]').val(dbconn.join(','));

    var dbconn_secondary = [];
    for(var i=0; i< $('#dbconn_secondary').next().find('ul li.search-choice').size(); i++){
        var name = $($('#dbconn_secondary').next().find('ul li.search-choice')[i]).text();
        dbconn_secondary.push($('#dbconn_secondary option:contains('+name+')').val());
    }
    $('[name=dbconn_secondary]').val(dbconn_secondary.join(','));

    var data = $('#form-api').serialize();
    $('#myModal .modal-body').empty();
    $('#myModal .modal-body').append('<center style="margin:50px 0"><i class="ace-icon fa fa-spinner fa-spin orange bigger-300"></i></center>')

    function callback(response){
        $('#myModal .modal-body').empty();
        $('#myModal .modal-body').append(response);
    }

    $.post('/management/api-test', data, callback);
}

function switchSourceType(){
    var sourceType = $.trim($(this).text());
    $('input[name=source_type]').val(sourceType.toUpperCase());

    $('.source-type').removeClass('label-success');
    $('.source-type').removeClass('label-default');
    $('.source-type').addClass('label-default');

    $(this).addClass('label-success');
    editor.getSession().setMode("ace/mode/"+sourceType.toLowerCase());
}


var editor = null;
$(document).ready(function(){
    editor = ace.edit("editor");
    editor.setTheme("ace/theme/xcode");
    // 显示 tap
    editor.setDisplayIndentGuides(true);
    var sourceType = $.trim($('.label-success').text());
    editor.getSession().setMode("ace/mode/"+sourceType.toLowerCase());
    editor.getSession().setUseSoftTabs(true);
    editor.getSession().setTabSize(4);
    // ctrl[command] + enter = hot key for run sql
    editor.commands.addCommand({
        name: 'runsql',
        bindKey: {win: "Ctrl-Enter", mac: "Command-Enter"},
        exec: function(editor){
            apiTest();
        }
    })

    // save
    $('#btn-save-api').click(saveApi);
    $('#form-api').submit(function(){ return false; });

    //new para
    $('#btn-new-para').click(newPara);
    //del para
    $('.delete-para').on('click', delPara);

    // api test
    $('#btn-api-test').click(apiTest)

    // switch source type
    $('.source-type').click(switchSourceType);

});

document.getElementById('form-field-description').addEventListener('keydown', function(e) {
    if (e.keyCode == 9 ) {
        e.preventDefault();
        this.setRangeText('\t');
        this.selectionEnd = ++this.selectionStart;
    }
});