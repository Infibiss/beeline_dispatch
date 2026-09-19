<?
  include "mysql.php";
  
  //include "config.php";

  function file_get_contents2($url) {
    for ($i=0; $i<3; $i++) {
      $agent= 'Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1; .NET CLR 1.0.3705; .NET CLR 1.1.4322)';
      $ch = curl_init();
      curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);
      curl_setopt($ch, CURLOPT_VERBOSE, true);
      curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
      curl_setopt($ch, CURLOPT_USERAGENT, $agent);
      curl_setopt($ch, CURLOPT_URL, $url);
      curl_setopt($ch, CURLOPT_TIMEOUT, 5);
      $return = curl_exec($ch);
      //print_r($return);
      if ($return === false) {
        echo "Error: ".curl_error($ch)."<br>";
      } else break;
    }
    //print_r(curl_getinfo($ch));
    curl_close($ch);
    return $return;
  }
  
  $px = 37.400070;
  $py = 55.953630;
  $step = 0.16;
  $month = date("Y-m");
  $date = date("Y-m-d-H");
  $time = date("Y-m-d H:i");
  $hour = date("Y-m-d H");
  mkdir("cache/$date");
  mkdir("result/$month");
  $show_fname = "cache/$date/show.htm";
  $fh = fopen($show_fname, "w");
  // Parse XML
  for ($i=0; $i<3; $i++) {
    $ri = file_get_contents2("http://export.yandex.ru/bar/reginfo.xml");
    $p = xml_parser_create();
    xml_parse_into_struct($p, $ri, $vals, $ind);
    xml_parser_free($p);
    echo "<pre>";
    $traf = $vals[$ind[LEVEL][0]][value];
    if (strlen($traf)>0) break;
  }
  file_put_contents("cache/$date/$date-reginfo.xml", $ri);
  file_put_contents("cache/$date/$date-weather.xml", file_get_contents2("http://export.yandex.ru/weather/?city=27612"));
  $we = file_get_contents2("http://export.yandex.ru/weather/?city=27612");
  file_put_contents("cache/$date/$date-weather.xml", $we);
  $p = xml_parser_create();
  xml_parse_into_struct($p, $we, $vals, $ind);
  //print_r($ind);
  //print_r($vals);
  xml_parser_free($p);
  $temp = $vals[$ind[TEMPERATURE][0]][value];
  $damp = $vals[$ind[DAMPNESS][0]][value];
  fputs($fh, "Traffic: $traf ");
  fputs($fh, "Temp: $temp ");
  fputs($fh, "Damp: $damp<br>");
  $numX = 2;
  $numY = 4;
  // Merge images
  $tileWidth = 600;
  $tileHeight = 450;
  $pxBetweenTiles = 0;
   
  $mapWidth = $tileWidth* $numX;
  $mapHeight = $tileHeight* $numY;
   
  $mapImage = imagecreate($mapWidth, $mapHeight);
  $color1 = imagecolorallocate($mapImage, 0, 0, 0);
  //imagefill($mapImae, 0, 0, $bgColor);  
  // Save images
  for ($y=0; $y<$numY; $y++) {
    $py2 = $py - $step*$y*1.095;
    for ($x=0; $x<$numX; $x++) {
      $px2 = $px + $step*$x*2.574;
      $url = "http://static-maps.yandex.ru/1.x/?ll=$px2,$py2&spn=$step,$step&l=map,trf";
      $fname = "cache/$date/$date-$x-$y.png";
      file_put_contents($fname, file_get_contents2($url));
      $tileImg = imagecreatefrompng($fname);
      imagecopy($mapImage, $tileImg, $x*$tileWidth, $y*$tileHeight, 0, 0, $tileWidth, $tileHeight);
      imagedestroy($tileImg);
      fputs($fh, "<img src=$date-$x-$y.png>");
    }
    fputs($fh, "<br>");
  }
  // Save map
  imagefttext($mapImage, 24, 0, 200, 40, $color, "fonts/arial.ttf", "$time Traffic: $traf Temp: $temp Damp: $damp");
  //imagestring($mapImage, 10, 400, 0, "$time Traffic: $traf Temp: $temp Damp: $damp", $color);
  imagepng($mapImage, "result/$month/$date.png", 9);
  fclose($fh);
  // Write to mysql
  mysql_query("REPLACE INTO hours VALUES('$hour:0:0', '$time:0', '$traf', '$temp', '0')");
  echo mysql_error();
  echo "<a href=$show_fname>SHOW RESULTS</a>";
?>