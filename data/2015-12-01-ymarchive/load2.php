<?
  // Loads information from iprobki.ru
  include "mysql.php";
  $sa = array(0, 1, 2, 301, 302);
  $ta = array(0 => array(186, 178),
              1 => array(199, 124),
              2 => array(200, 117),
              3 => array(200, 128),
              4 => array(180, 251),
              5 => array(186, 251),
              6 => array(187, 250),
              7 => array(110, 240),
              8 => array(107, 235),
              9 => array(112, 239),
              10 => array(96, 237),
              );

  function file_get_contents2($url) {
    for ($i=0; $i<1; $i++) {
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

  function put_image($fname, $x, $y) {
    GLOBAL $tileWidth, $tileHeight, $tileImg, $mapImage;
    $tileImg = imagecreatefrompng($fname);
    imagecopy($mapImage, $tileImg, $x*$tileWidth, $y*$tileHeight, 0, 0, imagesx($tileImg), imagesy($tileImg));
    imagedestroy($tileImg);
  }
  
  function analyze_traf_image($fname) {
    GLOBAL $ta;
    //$fname = "cache2/$y-$m2-$d2-$h2-00.gif";
    $img = imagecreatefrompng($fname);
    if ($img === false) continue;
    $ca = array();
    for ($x=22; $x<45; $x++) {
      for ($z=15; $z<40; $z++) {
        $ca[] = imagecolorsforindex($img, imagecolorat($img, $x, $z));
      }
    }
    //print_r($ca);
    // Make average
    $cav = array();
    $count = 0;
    foreach ($ca as $key => $val) {
      if ($val[blue] != 255) {
        $count++;
        $cav[green] += $val[green];
        $cav[red] += $val[red];
      }
    }
    $cav[green] = round($cav[green]/$count);
    $cav[red] = round($cav[red]/$count);
    // Find closest
    $cav2 = array();
    foreach ($ta as $key2 => $val2) {
      //echo "Compare $cav[green] to $val2[0] and $cav[red] to $val2[1]<br>";
      $cav2[$key2] = abs($cav[green]-$val2[0]) + abs($cav[red]-$val2[1]);
    }
    asort($cav2);
    foreach ($cav2 as $key => $val) {
      $traf = $key;
      break;
    }
    echo "$fname $cav[green] $cav[red] $traf <img src=$fname><br>";
    imagedestroy($img);
    //mysql_query("UPDATE hours SET traf='$traf' WHERE 
      //YEAR(hour)='$y' AND MONTH(hour)='$m' AND DAY(hour)='$d' AND HOUR(hour)='$h'
    //");
    //echo mysql_error();
    return $traf;
  }
  
  // Merge images
  $tileWidth = 256;
  $tileHeight = 256;
   
  $mapWidth = $tileWidth*2;
  $mapHeight = $tileHeight*3;
   
  $mapImage = imagecreate($mapWidth, $mapHeight);
  //$color1 = imagecolorallocate($mapImage, 255, 255, 255);

  $time = strtotime("2015-10-31");
  $sec = 1;
  for ($i=0; ; $i++) {
    $time += 3600;
    //$r = mysql_query("SELECT * FROM hours WHERE hour='".date("Y-m-d H", $time).":0:0'");
    //echo mysql_error();
    //$n = mysql_numrows($r);
    //if ($n>0) continue;
    $fname2 = "result/".date("Y-m", $time)."/".date("Y-m-d-H", $time).".png";
    if (file_exists($fname2)) continue;
    do {
      $fname = "cache2/".date("Y-m-d-H-1", $time).".gif";
      $url = "http://iprobki.ru/tmp/images/msk/".date("Ymd", $time)."/".($time+$sa[$sec])."/1.gif";
      //echo "$url<br>";
      $st = file_get_contents2($url);
      if (strlen($st)>2000) {
        //echo "Found<br>";
        file_put_contents($fname, $st);
        put_image($fname, 0, 0);
        for ($x=2; $x<7; $x++) {
          $url = "http://iprobki.ru/tmp/images/msk/".date("Ymd", $time)."/".($time+$sa[$sec])."/$x.gif";
          $fname = "cache2/".date("Y-m-d-H-$x", $time).".gif";
          file_put_contents($fname, file_get_contents2($url));
          put_image($fname, ($x-1)%2, round($x/2)-1);
        }
        $fname = "cache2/".date("Y-m-d-H-i", $time).".gif";
        $url = "http://iprobki.ru/tmp/images/msk/".date("Ymd", $time)."/".($time+$sa[$sec])."/informer.gif";
        echo "$url<br>";
        file_put_contents($fname, file_get_contents2($url));
        put_image($fname, 0, 0);
        $traf = analyze_traf_image($fname);
        $fname = "cache2/".date("Y-m-d-H", $time)."-w.png";
        $url = "http://iprobki.ru/tmp/images/msk/".date("Ymd", $time)."/".($time+$sa[$sec])."/weather.png";
        file_put_contents($fname, file_get_contents2($url));
        put_image($fname, 0, 2);
        mysql_query("INSERT INTO hours VALUES('".date("Y-m-d H", $time).":0:0', '".date("Y-m-d H:i:s", $time+$sa[$sec])."', '$traf', '$temp', '$damp')");
        echo mysql_error();
        mkdir("result/".date("Y-m", $time));
        imagepng($mapImage, $fname2, 9);
        break;
      }
      $sec++;
      if ($sec>5) {
        echo "Cannot find gif for $time $url<br>";
        break;
      }
    } while (1==1);
    $sec = 1;
    //if ($i>3000) break;
    //echo "Compare $time to ".time()."<br>";
    //break;
    if ($time > time()) break;
  }
?>