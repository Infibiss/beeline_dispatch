from field_router.apk_download import apk_response


def test_ranges_reconstruct_identical_apk_and_preserve_validator(tmp_path):
    path=tmp_path/'app.apk';data=b'PK\x03\x04'+bytes(range(256))*3;path.write_bytes(data)
    status,body,headers=apk_response(path)
    assert status==200 and body==data and headers['Accept-Ranges']=='bytes'
    first=apk_response(path,'bytes=0-99')
    rest=apk_response(path,'bytes=100-',headers['ETag'])
    assert first[0]==rest[0]==206
    assert first[1]+rest[1]==data
    assert first[2]['Content-Range']==f'bytes 0-99/{len(data)}'
    assert apk_response(path,'bytes=-17')[1]==data[-17:]
    assert apk_response(path,'bytes=0-999999')[1]==data
    assert apk_response(path,'bytes=100-','"old-build"')[:2]==(200,data)


def test_invalid_ranges_return_size_without_any_file_bytes(tmp_path):
    path=tmp_path/'app.apk';path.write_bytes(b'123456')
    for value in ('bytes=9-','bytes=4-2','bytes=-0','bytes=-','bytes=1-2,4-5','other=1-2','bytes='+'9'*201+'-'):
        status,body,headers=apk_response(path,value)
        assert status==416 and not body and headers['Content-Range']=='bytes */6'
