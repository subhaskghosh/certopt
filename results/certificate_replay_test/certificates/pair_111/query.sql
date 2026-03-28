SELECT
  t.firstname,
  t.lastname,
  e.city,
  e.state
FROM PERSON AS T
LEFT JOIN ADDRESS AS E
  ON e.personid = t.personid
WHERE
  T.PERSONID IN (
    SELECT
      PERSONID
    FROM PERSON
    GROUP BY
      PERSONID
  )